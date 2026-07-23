import logging
import ssl
import unicodedata
from asyncio import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    Queue,
    TimeoutError,
    create_task,
    current_task,
    gather,
    get_running_loop,
    shield,
    start_server,
    wait,
    wait_for,
)
from collections import defaultdict
from enum import Enum
from functools import partial, wraps
from pathlib import Path, PurePosixPath
from socket import AF_INET, AF_INET6
from stat import S_ISDIR, filemode
from time import gmtime, localtime, strftime, time

from .common import StreamIO, setlocale, wrap_with_container
from .errors import NoAvailablePort, PathIOError
from .pathio import PathIONursery

logger = logging.getLogger("NebulaFTP")

__all__ = (
    "Permission", "User", "AbstractUserManager", "MongoDBUserManager",
    "Connection", "AvailableConnections", "ConnectionConditions",
    "PathConditions", "PathPermissions", "worker", "Server",
)

class Permission:
    def __init__(self, path="/", *, readable=False, writable=False):
        self.path = PurePosixPath(path); self.readable = readable or writable; self.writable = writable
    def is_parent(self, other):
        try: other.relative_to(self.path); return True
        except ValueError: return False

class User:
    def __init__(self, login, password, permissions=[]):
        # `password` may be a plaintext string (legacy), a bcrypt hash, or both
        # depending on how it was loaded.
        self.login = login
        self._raw_password = password
        self.password_hash = password if isinstance(password, str) and password.startswith(("$2a$", "$2b$", "$2y$")) else ""
        self.base_path = Path(".")
        self.home_path = PurePosixPath(f"/{login}")
        self.permissions = [Permission(f"/{login}", readable=True, writable=True)] + permissions
        if not [p for p in self.permissions if p.path == PurePosixPath("/")]:
            self.permissions.append(Permission("/", readable=False, writable=False))

    def check_password(self, plain: str) -> bool:
        """Constant-time verify. Accepts legacy plaintext for backwards compat."""
        from .auth import verify_password
        return verify_password(plain, self._raw_password)
    def get_permissions(self, path):
        path = PurePosixPath(path)
        parents = filter(lambda p: p.is_parent(path), self.permissions)
        return min(parents, key=lambda p: len(path.relative_to(p.path).parts), default=Permission())
    def update(self, d):
        self._raw_password = d._raw_password or self._raw_password; self.permissions.clear()
        self.password_hash = d.password_hash or self.password_hash
        self.permissions = [Permission(f"/{self.login}", readable=True, writable=True)]
        for perm in d.permissions: self.permissions.append(perm)
        return self
    @classmethod
    def from_dict(cls, d):
        login = d["login"]; permissions = []
        # Accept both new schema (password_hash) and legacy (password)
        password_field = d.get("password_hash") or d.get("password") or ""
        for perm in d.get("permissions", []):
            if perm["path"] != f"/{login}":
                perm["path"] = perm["path"].strip(); permissions.append(Permission(**perm))
        return cls(login, password_field, permissions)

class AbstractUserManager:
    GetUserResponse = Enum("UserManagerResponse", "PASSWORD_REQUIRED ERROR")

class MongoDBUserManager(AbstractUserManager):
    def __init__(self, db):
        self.db = db; self.available_connections = {}; self.users = []
    async def get_user(self, login):
        doc = await self.db.users.find_one({"login": login})
        user = User.from_dict(doc) if doc else None
        if user:
            u = [usr for usr in self.users if usr.login == user.login]
            if u: user = u[0].update(user)
            else: self.users.append(user)
            if user.login not in self.available_connections:
                self.available_connections[user] = AvailableConnections(100)
        if not user: state, info = AbstractUserManager.GetUserResponse.ERROR, "no such username"
        elif self.available_connections[user].locked(): state, info = AbstractUserManager.GetUserResponse.ERROR, f"too much connections"
        else: state, info = AbstractUserManager.GetUserResponse.PASSWORD_REQUIRED, "password required"
        if state != AbstractUserManager.GetUserResponse.ERROR: self.available_connections[user].acquire()
        return state, user, info
    async def authenticate(self, user, password):
        if not password:
            return False
        if user.check_password(password):
            # Migrate legacy plaintext rows on first successful login.
            if not user.password_hash:
                try:
                    from .auth import hash_password
                    new_hash = hash_password(password)
                    await self.db.users.update_one(
                        {"login": user.login},
                        {"$set": {"password_hash": new_hash}, "$unset": {"password": ""}},
                    )
                    user.password_hash = new_hash
                except Exception:
                    pass  # Migration failures don't block login
            return True
        return False
    async def notify_logout(self, user):
        if user in self.available_connections: self.available_connections[user].release()
        self.users = [u for u in self.users if u.login != user.login]

class Connection(defaultdict):
    __slots__ = ("future",)
    class Container:
        def __init__(self, storage): self.storage = storage
        def __getattr__(self, name): return self.storage[name]
        def __delattr__(self, name): self.storage.pop(name)
    def __init__(self, **kwargs):
        super().__init__(Future); self.future = Connection.Container(self)
        for k, v in kwargs.items(): self[k].set_result(v)
    def __getattr__(self, name):
        if name in self: return self[name].result()
        raise AttributeError(f"{name!r} not in storage")
    def __setattr__(self, name, value):
        if name in Connection.__slots__: super().__setattr__(name, value)
        else:
            if self[name].done(): self[name] = super().default_factory()
            self[name].set_result(value)
    def __delattr__(self, name):
        if name in self: self.pop(name)

class AvailableConnections:
    def __init__(self, value=None): self.value = self.maximum_value = value
    def locked(self): return self.value is not None and self.value <= 0
    def acquire(self):
        if self.value is not None:
            self.value -= 1
            if self.value < 0: self.value = 0; raise ValueError("Too many acquires")
    def release(self):
        if self.value is not None:
            self.value += 1
            if self.value > self.maximum_value: self.value = self.maximum_value

class ConnectionConditions:
    user_required = ("user", "no user")
    login_required = ("logged", "not logged in")
    passive_server_started = ("passive_server", "no listen socket")
    data_connection_made = ("data_connection", "no data connection")
    rename_from_required = ("rename_from", "no filename")
    def __init__(self, *fields, wait=False, fail_code="503", fail_info=None):
        self.fields = fields; self.wait = wait; self.fail_code = fail_code; self.fail_info = fail_info
    def __call__(self, f):
        @wraps(f)
        async def wrapper(cls, connection, rest, *args):
            futures = {connection[name]: msg for name, msg in self.fields}
            aggregate = gather(*futures); timeout = 1 if self.wait else 0
            try: await wait_for(shield(aggregate), timeout)
            except TimeoutError:
                for future, message in futures.items():
                    if not future.done():
                        connection.response(self.fail_code, self.fail_info or f"bad sequence ({message})")
                        return True
            return await f(cls, connection, rest, *args)
        return wrapper

class PathConditions:
    path_must_exists = ("exists", False, "path does not exists")
    path_must_not_exists = ("exists", True, "path already exists")
    path_must_be_dir = ("is_dir", False, "path is not a directory")
    path_must_be_file = ("is_file", False, "path is not a file")
    def __init__(self, *conditions): self.conditions = conditions
    def __call__(self, f):
        @wraps(f)
        async def wrapper(cls, connection, rest, *args):
            real_path, virtual_path = cls.get_paths(connection, rest)
            for name, fail, message in self.conditions:
                if await getattr(connection.path_io, name)(real_path) == fail:
                    connection.response("550", message); return True
            return await f(cls, connection, rest, *args)
        return wrapper

class PathPermissions:
    readable = "readable"; writable = "writable"
    def __init__(self, *permissions): self.permissions = permissions
    def __call__(self, f):
        @wraps(f)
        async def wrapper(cls, connection, rest, *args):
            real_path, virtual_path = cls.get_paths(connection, rest)
            perm = connection.user.get_permissions(virtual_path)
            for p in self.permissions:
                if not getattr(perm, p): connection.response("550", "permission denied"); return True
            return await f(cls, connection, rest, *args)
        return wrapper

def worker(f):
    @wraps(f)
    async def wrapper(cls, connection, rest):
        try: await f(cls, connection, rest)
        except CancelledError: connection.response("426", "transfer aborted"); connection.response("226", "abort successful")
        except Exception as exc:
            logger.debug("worker failed: %s", exc)
            connection.response("451", "transfer error")
    return wrapper

class Server:
    def __init__(self, user_manager, path_io):
        self.path_io_factory = PathIONursery(path_io); self.user_manager = user_manager
        self.available_connections = AvailableConnections(256)
        self.ssl_context = None
        self.commands_mapping = {
            "allo": self.allo, "auth": self.auth, "ccc": self.ccc, "abor": self.abor, "appe": self.appe, "cdup": self.cdup, "cwd": self.cwd,
            "dele": self.dele, "epsv": self.epsv, "feat": self.feat, "list": self.list, "mdtm": self.mdtm,
            "mfmt": self.mfmt, "mkd": self.mkd, "mlsd": self.mlsd, "mlst": self.mlst, "noop": self.noop,
            "opts": self.opts, "pass": self.pass_, "pasv": self.pasv, "pbsz": self.pbsz, "prot": self.prot, "pwd": self.pwd, "quit": self.quit,
            "rest": self.rest, "retr": self.retr, "rmd": self.rmd, "rnfr": self.rnfr,
            "rnto": self.rnto, "site": self.site, "size": self.size, "stor": self.stor, "syst": self.syst, "type": self.type,
            "user": self.user,
        }

    def set_ssl_context(self, ssl_context):
        self.ssl_context = ssl_context

    def _build_ssl_context(self, certfile, keyfile, require_client_cert=False):
        if not certfile or not keyfile:
            return None
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        try:
            ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
        except FileNotFoundError as exc:
            logger.error("TLS cert/key not found: %s", exc)
            raise
        except ssl.SSLError as exc:
            logger.error("TLS cert load failed: %s", exc)
            raise
        if require_client_cert:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_default_certs()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    async def start(self, host="0.0.0.0", port=9021, **kwargs):
        self._start_server_extra_arguments = kwargs; self.connections = {}
        self.server_host = host; self.server_port = port
        self.server = await start_server(self.dispatcher, host, port, ssl=self.ssl_context, **kwargs)
        for sock in self.server.sockets:
            if sock.family in (AF_INET, AF_INET6):
                h, p, *_ = sock.getsockname()
                if not self.server_port: self.server_port = p

    async def serve_forever(self): await self.server.serve_forever()
    async def run(self, host="0.0.0.0", port=9021, **kwargs):
        await self.start(host, port, **kwargs)
        try: await self.serve_forever()
        finally: await self.close()

    async def close(self):
        self.server.close()
        tasks = [create_task(self.server.wait_closed())]
        for conn in self.connections.values(): conn._dispatcher.cancel(); tasks.append(conn._dispatcher)
        await wait(tasks)

    # ✅ CORREÇÃO: Força Encoding UTF-8 na SAÍDA
    async def write_line(self, stream, line):
        try:
            encoded = (line + "\r\n").encode("utf-8")
        except UnicodeEncodeError:
            # Fallback: Sanitização
            normalized = unicodedata.normalize('NFC', str(line))
            clean = ''.join(c for c in normalized if unicodedata.category(c) != 'Cc' or c in '\r\n\t')
            encoded = (clean + "\r\n").encode("utf-8", errors='ignore')
        await stream.write(encoded)

    async def write_response(self, stream, code, lines="", list=False):
        lines = wrap_with_container(lines); write = partial(self.write_line, stream)
        if list:
            await write(code + "-" + lines[0])
            for line in lines[1:-1]: await write(" " + line)
            await write(code + " " + lines[-1])
        else:
            for line in lines[:-1]: await write(code + "-" + line)
            await write(code + " " + lines[-1])

    # ✅ CORREÇÃO: Força Decoding UTF-8 na ENTRADA
    async def parse_command(self, stream):
        line = await stream.readline()
        if not line: raise ConnectionResetError
        
        # Tenta decodificar UTF-8 (Padrão Rclone/RaiDrive)
        try:
            s = line.decode('utf-8').rstrip()
        except UnicodeDecodeError:
            # Fallback para Latin-1 (FileZilla antigo em Windows)
            try:
                s = line.decode('latin-1').rstrip()
            except:
                s = line.decode('utf-8', errors='ignore').rstrip()
        
        # Normalização Unicode (Crucial para acentos)
        s = unicodedata.normalize('NFC', s)
        
        cmd, _, rest = s.partition(" ")
        return cmd.lower(), rest

    async def response_writer(self, stream, queue):
        while True:
            args = await queue.get()
            try: await self.write_response(stream, *args)
            finally: queue.task_done()

    async def dispatcher(self, reader, writer):
        stream = StreamIO(reader, writer)
        host, port, *_ = writer.transport.get_extra_info("peername", ("", ""))
        key = stream
        queue = Queue()
        conn = Connection(
            server_host=writer.transport.get_extra_info("sockname")[0],
            server_port=self.server_port,
            command_connection=stream,
            path_io_factory=self.path_io_factory,
            extra_workers=set(),
            response=lambda *args: queue.put_nowait(args),
            acquired=False, restart_offset=0, _dispatcher=current_task()
        )
        conn.path_io = self.path_io_factory(connection=conn)
        pending = {create_task(self.greeting(conn, "")), create_task(self.response_writer(stream, queue)), create_task(self.parse_command(stream))}
        self.connections[key] = conn
        try:
            while True:
                done, pending = await wait(pending | conn.extra_workers, return_when=FIRST_COMPLETED)
                conn.extra_workers -= done
                for task in done:
                    try: res = task.result()
                    except PathIOError: conn.response("451", "fs error"); continue
                    except ConnectionResetError: return
                    if isinstance(res, bool):
                        if not res: await queue.join(); return
                    elif isinstance(res, tuple):
                        pending.add(create_task(self.parse_command(stream)))
                        cmd, rest = res
                        f = self.commands_mapping.get(cmd)
                        if f:
                            pending.add(create_task(f(conn, rest)))
                            if cmd not in ("retr", "stor", "appe"): conn.restart_offset = 0
                        else: conn.response("502", "not implemented")
        except CancelledError: raise
        except (ConnectionResetError, BrokenPipeError) as exc:
            logger.debug("dispatcher closed cleanly: %s", exc)
        except Exception:
            logger.exception("dispatcher crashed for %s:%s", host, port)
        finally:
            tasks = []
            if not get_running_loop().is_closed():
                for t in pending | conn.extra_workers: t.cancel(); tasks.append(t)
                if conn.future.passive_server.done(): conn.passive_server.close()
                if conn.future.data_connection.done(): conn.data_connection.close()
                stream.close()
            if conn.acquired: self.available_connections.release()
            if conn.future.user.done(): tasks.append(create_task(self.user_manager.notify_logout(conn.user)))
            if key in self.connections: self.connections.pop(key)
            if tasks: await wait(tasks)

    @staticmethod
    def get_paths(connection, path):
        virtual = PurePosixPath(path)
        if not virtual.is_absolute(): virtual = connection.current_directory / virtual
        resolved = PurePosixPath("/")
        for part in virtual.parts[1:]:
            if part == "..": resolved = resolved.parent
            else: resolved /= part
        if connection.future.user.done():
            home = connection.user.home_path
            try:
                resolved.relative_to(home)
            except ValueError:
                resolved = home / resolved.relative_to("/")
        base = connection.user.base_path
        try: real = base / resolved.relative_to("/")
        except ValueError: real = base; resolved = PurePosixPath("/")
        return real, resolved

    async def greeting(self, conn, rest):
        if self.available_connections.locked(): ok, c, i = False, "421", "Busy"
        else: ok, c, i = True, "220", "Nebula FTP"; conn.acquired = True; self.available_connections.acquire()
        conn.response(c, i); return ok

    async def user(self, conn, rest):
        if conn.future.user.done(): await self.user_manager.notify_logout(conn.user)
        del conn.user; del conn.logged
        state, user, info = await self.user_manager.get_user(rest)
        if state == AbstractUserManager.GetUserResponse.PASSWORD_REQUIRED: code = "331"; conn.user = user
        elif state == AbstractUserManager.GetUserResponse.ERROR: code = "530"
        if conn.future.user.done(): conn.current_directory = conn.user.home_path
        conn.response(code, info); return True

    @ConnectionConditions(ConnectionConditions.user_required)
    async def pass_(self, conn, rest):
        if conn.future.logged.done(): code, info = "503", "already logged"
        elif await self.user_manager.authenticate(conn.user, rest):
            conn.logged = True; code, info = "230", "ok"
            conn.current_directory = conn.user.home_path
            await conn.path_io.mkdir(conn.user.home_path, exist_ok=True)
        else: code, info = "530", "wrong pass"
        conn.response(code, info); return True

    async def quit(self, conn, rest): conn.response("221", "bye"); return False
    
    @ConnectionConditions(ConnectionConditions.login_required)
    async def pwd(self, conn, rest): conn.response("257", f"\"{conn.current_directory}\""); return True

    async def feat(self, c, r):
        c.response("211", ["start", "UTF8", "PASV", "EPSV", "MLST Type*;Size*;Modify*;", "MDTM", "MFMT", "SIZE", "end"], True); return True

    async def noop(self, c, r): c.response("200", "ok"); return True
    async def allo(self, c, r): c.response("200", "ok"); return True
    async def opts(self, c, r): c.response("200", "ok"); return True
    async def site(self, c, r): c.response("200", "ok"); return True

    @ConnectionConditions(ConnectionConditions.login_required)
    async def size(self, conn, rest):
        real, virt = self.get_paths(conn, rest)
        try:
            stats = await conn.path_io.stat(real)
        except PathIOError:
            conn.response("213", "0"); return True
        conn.response("213", str(stats.st_size)); return True

    @ConnectionConditions(ConnectionConditions.login_required)
    @PathPermissions(PathPermissions.readable)
    async def cwd(self, conn, rest):
        real, virt = self.get_paths(conn, rest)
        if not await conn.path_io.is_dir(real):
            await conn.path_io.mkdir(real, exist_ok=True)
        conn.current_directory = virt; conn.response("250", "ok"); return True

    @ConnectionConditions(ConnectionConditions.login_required)
    async def cdup(self, conn, rest): return await self.cwd(conn, conn.current_directory.parent)

    @ConnectionConditions(ConnectionConditions.login_required)
    @PathPermissions(PathPermissions.writable)
    async def mkd(self, conn, rest):
        real, virt = self.get_paths(conn, rest); await conn.path_io.mkdir(real, exist_ok=True); conn.response("257", "ok"); return True

    @ConnectionConditions(ConnectionConditions.login_required)
    @PathPermissions(PathPermissions.writable)
    async def rmd(self, conn, rest):
        real, virt = self.get_paths(conn, rest)
        try:
            await conn.path_io.rmdir(real)
        except PathIOError as exc:
            logger.debug("rmd ignored (%s): %s", rest, exc)
        conn.response("250", "ok"); return True

    @ConnectionConditions(ConnectionConditions.login_required, ConnectionConditions.passive_server_started)
    @PathPermissions(PathPermissions.readable)
    async def list(self, conn, rest):
        @ConnectionConditions(ConnectionConditions.data_connection_made, wait=True, fail_code="425")
        @worker
        async def list_worker(self, conn, rest):
            stream = conn.data_connection; del conn.data_connection
            async with stream:
                async for path in conn.path_io.list(real):
                    s = await self.build_list_string(conn, path)
                    await stream.write((s + "\r\n").encode("utf-8"))
            conn.response("226", "done"); return True
        real, virt = self.get_paths(conn, rest)
        if not await conn.path_io.is_dir(real):
            await conn.path_io.mkdir(real, exist_ok=True)
        t = create_task(list_worker(self, conn, rest)); conn.extra_workers.add(t)
        conn.response("150", "listing"); return True

    async def build_list_string(self, conn, path):
        stats = await conn.path_io.stat(path)
        mtime = localtime(stats.st_mtime)
        with setlocale("C"):
            s = strftime("%b %e %H:%M", mtime) if time() - 15778476 < stats.st_mtime <= time() else strftime("%b %e  %Y", mtime)
        return " ".join((filemode(stats.st_mode), str(stats.st_nlink), "none", "none", str(stats.st_size), s, path.name))

    async def build_mlsd_string(self, conn, path):
        stats = await conn.path_io.stat(path)
        kind = "dir" if S_ISDIR(stats.st_mode) else "file"
        modified = strftime("%Y%m%d%H%M%S", gmtime(stats.st_mtime))
        return f"type={kind};size={stats.st_size};modify={modified}; {path.name}"

    @ConnectionConditions(ConnectionConditions.login_required)
    @PathPermissions(PathPermissions.readable)
    async def mlst(self, conn, rest):
        real, virt = self.get_paths(conn, rest)
        try:
            stats = await conn.path_io.stat(real)
            kind = "dir" if S_ISDIR(stats.st_mode) else "file"
            info = f"Type={kind};Size={stats.st_size}; {real.name}"
        except PathIOError:
            info = f"Type=file;Size=0; {real.name}"
        conn.response("250", ["start", info, "end"], True); return True

    @ConnectionConditions(ConnectionConditions.login_required)
    async def mdtm(self, conn, rest):
        real, virt = self.get_paths(conn, rest)
        try:
            stats = await conn.path_io.stat(real)
        except PathIOError:
            conn.response("213", strftime("%Y%m%d%H%M%S", gmtime(time()))); return True
        conn.response("213", strftime("%Y%m%d%H%M%S", gmtime(stats.st_mtime))); return True

    @ConnectionConditions(ConnectionConditions.login_required)
    async def mfmt(self, conn, rest):
        _timestamp, _sep, path = rest.partition(" ")
        real, virt = self.get_paths(conn, path or rest)
        conn.response("213", "modify ok"); return True

    @ConnectionConditions(ConnectionConditions.login_required)
    @PathPermissions(PathPermissions.writable)
    async def dele(self, conn, rest):
        real, virt = self.get_paths(conn, rest)
        try:
            await conn.path_io.unlink(real)
        except PathIOError as exc:
            logger.debug("dele ignored (%s): %s", rest, exc)
        conn.response("250", "deleted"); return True

    @ConnectionConditions(ConnectionConditions.login_required, ConnectionConditions.passive_server_started)
    @PathPermissions(PathPermissions.writable)
    async def stor(self, conn, rest, mode="wb"):
        @ConnectionConditions(ConnectionConditions.data_connection_made, wait=True, fail_code="425")
        @worker
        async def stor_worker(self, conn, rest):
            stream = conn.data_connection; del conn.data_connection
            mode_ = "r+b" if conn.restart_offset else mode
            file_out = await conn.path_io.open(real, mode=mode_)
            async with file_out, stream:
                if conn.restart_offset: await file_out.seek(conn.restart_offset)
                await file_out.write_stream(stream)
            conn.response("226", "transfer complete"); return True
        real, virt = self.get_paths(conn, rest)
        parent = real.parent
        parents = []
        while str(parent) not in ("", "."):
            parents.append(parent)
            if parent == parent.parent:
                break
            parent = parent.parent
        for folder in reversed(parents):
            await conn.path_io.mkdir(folder, exist_ok=True)
        if await conn.path_io.is_dir(real.parent):
            t = create_task(stor_worker(self, conn, rest)); conn.extra_workers.add(t)
            conn.response("150", "upload starting")
        else: conn.response("550", "path invalid")
        return True

    @ConnectionConditions(ConnectionConditions.login_required, ConnectionConditions.passive_server_started)
    @PathConditions(PathConditions.path_must_exists, PathConditions.path_must_be_file)
    @PathPermissions(PathPermissions.readable)
    async def retr(self, conn, rest):
        @ConnectionConditions(ConnectionConditions.data_connection_made, wait=True, fail_code="425")
        @worker
        async def retr_worker(self, conn, rest):
            stream = conn.data_connection; del conn.data_connection
            file_in = await conn.path_io.open(real, mode="rb")
            async with file_in, stream:
                if conn.restart_offset: await file_in.seek(conn.restart_offset)
                async for data in file_in.iter_by_block(1024 * 512):
                    await stream.write(data)
            conn.response("226", "transfer complete"); return True
        real, virt = self.get_paths(conn, rest)
        t = create_task(retr_worker(self, conn, rest)); conn.extra_workers.add(t)
        conn.response("150", "download starting"); return True

    async def type(self, c, r): c.response("200", "ok"); return True
    async def pbsz(self, c, r):
        if self.ssl_context is None and not c.future.tls_upgraded.done():
            c.response("503", "PBSZ not allowed without TLS")
            return True
        c.tls_upgraded = True; c.response("200", "ok"); return True
    async def prot(self, c, r):
        if r.strip().upper() == "P":
            c.protected = True; c.response("200", "ok")
        elif r.strip().upper() == "C":
            c.protected = False; c.response("200", "ok")
        else:
            c.response("504", "only P/C accepted")
        return True
    async def auth(self, c, r):
        if self.ssl_context is None:
            c.response("431", "TLS not configured"); return True
        try:
            await c.command_connection.writer.start_tls(self.ssl_context)
            c.tls_upgraded = True; c.response("234", "TLS active")
        except (ssl.SSLError, ConnectionError, OSError) as exc:
            logger.warning("AUTH TLS failed: %s", exc); c.response("431", "TLS handshake failed")
        return True
    async def ccc(self, c, r):
        c.protected = False; c.response("200", "ok"); return True
    async def syst(self, c, r): c.response("215", "UNIX Type: L8"); return True
    async def pasv(self, c, r): return await self._pasv_common(c, False)
    async def epsv(self, c, r): return await self._pasv_common(c, True)
    
    async def _pasv_common(self, conn, epsv):
        async def h(r, w):
            if conn.future.data_connection.done(): w.close()
            else: conn.data_connection = StreamIO(r, w)
        if not conn.future.passive_server.done():
            try: conn.passive_server = await start_server(h, conn.server_host, 0, ssl=self.ssl_context, **self._start_server_extra_arguments)
            except NoAvailablePort: conn.response("421", "no ports"); return False
            except OSError as exc:
                logger.warning("Passive server bind failed: %s", exc); conn.response("421", "no ports"); return False
        
        for s in conn.passive_server.sockets:
            if s.family == AF_INET: host, port = s.getsockname(); break
        else: host, port = "127.0.0.1", 0
        
        if epsv: msg = f"entering epsv (|||{port}|)"
        else:
            p1, p2 = port >> 8, port & 0xff
            h = host.replace(".", ",")
            msg = f"entering pasv ({h},{p1},{p2})"
        
        if conn.future.data_connection.done(): conn.data_connection.close(); del conn.data_connection
        conn.response("229" if epsv else "227", msg)
        return True

    async def abor(self, conn, rest):
        if conn.extra_workers:
            for w in conn.extra_workers: w.cancel()
        conn.response("226", "abor"); return True
    async def appe(self, c, r): return await self.stor(c, r, "ab")
    async def rest(self, c, r): c.restart_offset = int(r) if r.isdigit() else 0; c.response("350", "restart"); return True
    async def rnfr(self, c, r): c.rename_from = self.get_paths(c, r)[0]; c.response("350", "pending"); return True
    async def rnto(self, c, r):
        real, virt = self.get_paths(c, r); rename = c.rename_from; del c.rename_from
        try:
            await c.path_io.rename(rename, real)
        except PathIOError as exc:
            logger.debug("rename ignored (%s -> %s): %s", rename, real, exc)
        c.response("250", "renamed"); return True
    @ConnectionConditions(ConnectionConditions.login_required, ConnectionConditions.passive_server_started)
    @PathPermissions(PathPermissions.readable)
    async def mlsd(self, conn, rest):
        @ConnectionConditions(ConnectionConditions.data_connection_made, wait=True, fail_code="425")
        @worker
        async def mlsd_worker(self, conn, rest):
            stream = conn.data_connection; del conn.data_connection
            async with stream:
                async for path in conn.path_io.list(real):
                    s = await self.build_mlsd_string(conn, path)
                    await stream.write((s + "\r\n").encode("utf-8"))
            conn.response("226", "done"); return True
        real, virt = self.get_paths(conn, rest)
        if not await conn.path_io.is_dir(real):
            await conn.path_io.mkdir(real, exist_ok=True)
        t = create_task(mlsd_worker(self, conn, rest)); conn.extra_workers.add(t)
        conn.response("150", "listing"); return True
