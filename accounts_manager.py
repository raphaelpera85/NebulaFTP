from os import environ
from os.path import exists
from pathlib import PurePosixPath
from re import compile
import argparse
import getpass
import sys

if exists(".env"):
    from dotenv import load_dotenv
    load_dotenv()

from ftp.auth import hash_password, verify_password, is_hashed

login_regex = compile(r'^[a-zA-Z0-9_]{1,64}$')

BCRYPT_ROUNDS = int(environ.get("BCRYPT_ROUNDS", "12"))
db = None


def get_db():
    global db
    if db is not None:
        return db
    try:
        from pymongo import MongoClient
        db_uri = environ.get("MONGODB") or input("MongoDB connect string: ")
        db = MongoClient(db_uri)[environ.get("MONGO_DATABASE", "ftp")]["users"]
        return db
    except Exception as exc:
        print(f"FATAL: MongoDB unreachable: {exc}", file=sys.stderr)
        sys.exit(2)


class Permission:
    def __init__(self, path, readable=False, writable=False):
        self.path = path
        self.read = readable or writable
        self.write = writable

class User:
    def __init__(self, login, password_hash, permissions):
        self.login = login
        self.password_hash = password_hash
        self.permissions = permissions

    def check_password(self, plain: str) -> bool:
        return verify_password(plain, self.password_hash)

    def formatPermissions(self):
        for perm in self.permissions:
            print(f"Path: {repr(perm.path)}, Read: {perm.read}, Write: {perm.write}")

    def addPermission(self, perm):
        self.permissions.append(perm)

    def removePermission(self, perm):
        self.permissions.remove(perm)

def getInput(arr, objs=None):
    o = bool(objs) and len(arr) == len(objs)
    while True:
        for i, line in enumerate(arr):
            print(f"{i+1}. {line}")
        try:
            _sel = input("Select: ")
            sel = int(_sel)
            if sel > len(arr) or sel <= 0:
                raise ValueError
            return sel-1 if not o else objs[sel-1]
        except ValueError:
            print(f"Invalid input: {_sel}")

def changeUserPassword(user):
    db = get_db()
    new_pass1 = getpass.getpass("Nova senha: ")
    new_pass2 = getpass.getpass("Repita a nova senha: ")
    if new_pass1 != new_pass2:
        print("As senhas não conferem")
        return
    try:
        hashed = hash_password(new_pass1)
    except ValueError as exc:
        print(f"Erro: {exc}")
        return
    if verify_password(new_pass1, user.password_hash):
        print("A nova senha não pode ser igual à anterior")
        return
    db.update_one(
        {"login": user.login},
        {"$set": {"password_hash": hashed}, "$unset": {"password": ""}},
    )
    user.password_hash = hashed
    print("Senha alterada com sucesso.")

def editPermissions(user):
    db = get_db()
    while True:
        print("Action:")
        action = getInput(["Add permission", "Edit permission", "Delete permission", "Back"])
        if action == 0:
            path = input("Path: ")
            path = PurePosixPath(path)
            if not path or not path.is_absolute() or [p for p in user.permissions if PurePosixPath(p.path) == path]:
                print("Invalid path")
                continue
            read = input("Read permission (yes/no): ").lower().strip() in ["yes", "y", "true", "1"]
            write = input("Write permission (yes/no): ").lower().strip() in ["yes", "y", "true", "1"]
            db.update_one({"login": user.login}, {"$push": {"permissions": {"path": str(path), "readable": read, "writable": write}}})
            perm = Permission(str(path), read, write)
            user.addPermission(perm)
            p = f"Path: {repr(perm.path)}, Read: {perm.read}, Write: {perm.write}"
            print(f"Permission \"{p}\" for user {user.login} created.")
            continue
        elif action in [1, 2]:
            print("Permissions:")
            perms = user.permissions.copy()
            perms = [f"Path: {repr(p.path)}, Read: {p.read}, Write: {p.write}" for p in perms]
            perms.append("Back")
            perm = getInput(perms, user.permissions.copy()+[None])
            if perm is None:
                continue
            if action == 1:
                ch = getInput(["Read", "Write", "Back"])
                read = perm.read
                write = perm.write
                if ch == 0:
                    read = input("Read permission (yes/no): ").lower().strip() in ["yes", "y", "true", "1"]
                elif ch == 1:
                    write = input("Write permission (yes/no): ").lower().strip() in ["yes", "y", "true", "1"]
                elif ch == 2:
                    continue
                idx = user.permissions.index(perm)
                db.update_one({"login": user.login}, {"$set": {f"permissions.{idx}.readable": read, f"permissions.{idx}.writable": write}})
                perm.read = read
                perm.write = write
                continue
            elif action == 2:
                if input("Write 'delete' to delete permission: ") != "delete":
                    print("Invalid input")
                    continue
                db.update_one({"login": user.login}, {"$pull": {"permissions": {"path": perm.path}}})
                user.removePermission(perm)
                print("Permission deleted")
        elif action == 3:
            return

def printUserData(user):
    get_db()
    while True:
        print(f"Login: {user.login}")
        print("Password: ******** (hashed)")
        print("Actions:")
        action = getInput(["Set password", "Show permissions", "Edit permissions", "Delete user", "Back"])
        if action == 0:
            changeUserPassword(user)
            continue
        elif action == 1:
            print("Permissions:")
            user.formatPermissions()
            print(f"Press enter to continue...")
            input()
            continue
        elif action == 2:
            editPermissions(user)
            continue
        elif action == 3:
            login = input(f"Enter '{user.login}' or 'delete user' to delete this user: ")
            if login != user.login and login != "delete user":
                print("Invalid input.")
                continue
            db.delete_one({"login": user.login})
            return
        elif action == 4:
            return

def showUsers():
    db = get_db()
    while True:
        print("Loading...", end="")
        _users = db.find({})
        print("\rUsers:     ")
        users = []
        for _user in _users:
            perms = [Permission(**perm) for perm in _user.get("permissions", [])]
            users.append(User(_user["login"], _user.get("password_hash", _user.get("password", "")), perms))
        t_users = [user.login for user in users]+["Back"]
        u = getInput(t_users, users.copy()+[None])
        if u is None:
            return
        printUserData(u)

def addUser():
    db = get_db()
    login = input("Login: ")
    if not login_regex.match(login):
        print("Login can include only this characters: \"a-Z, 0-9, _\" and have lenght <= 64")
        return
    if db.find_one({"login": login}):
        print("User with this login already exists")
        return
    plain = getpass.getpass("Password: ")
    plain2 = getpass.getpass("Repeat password: ")
    if plain != plain2:
        print("Passwords do not match"); return
    if not plain:
        print("Empty password rejected"); return
    try:
        hashed = hash_password(plain)
    except ValueError as exc:
        print(f"FATAL: {exc}"); return
    db.insert_one({"login": login, "password_hash": hashed, "permissions": []})
    print(f'User "{login}" created')


def cli_set_password(args):
    db = get_db()
    if not login_regex.match(args.login or ""):
        print("Invalid login"); sys.exit(2)
    plain = sys.stdin.readline().rstrip("\n") if args.password is None else args.password
    if not plain:
        print("Empty password rejected (use --password to provide non-interactively)"); sys.exit(2)
    if not db.find_one({"login": args.login}):
        print("No such user"); sys.exit(3)
    hashed = hash_password(plain)
    db.update_one(
        {"login": args.login},
        {"$set": {"password_hash": hashed}, "$unset": {"password": ""}},
    )
    print(f"Password updated for {args.login}")


def cli_list_users(_args):
    db = get_db()
    for u in db.find({}, {"login": 1, "_id": 0}):
        print(u["login"])


def cli_delete_user(args):
    db = get_db()
    res = db.delete_one({"login": args.login})
    print("deleted" if res.deleted_count else "no such user")
    sys.exit(0 if res.deleted_count else 4)


def cli_migrate_passwords(_args):
    db = get_db()
    """Re-hash any leftover plaintext passwords on next login.

    The server's `User.from_dict` already normalizes password_hash; this CLI
    also forces re-hashing of legacy rows to avoid silent foot-guns in
    shared environments."""
    count = 0
    for u in db.find({}):
        h = u.get("password_hash", u.get("password", ""))
        if not is_hashed(h):
            print(f"  legacy plaintext found for {u['login']} — manual reset required")
            count += 1
    if count:
        print(f"Found {count} legacy accounts. Reset passwords with:")
        print("  python accounts_manager.py set-password --login <user> --password <new>")
    else:
        print("All stored passwords are bcrypt-hashed.")

def main():
    parser = argparse.ArgumentParser(description="Nebula FTP account manager")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list", help="list logins")
    p_add = sub.add_parser("add", help="interactive add user")
    p_set = sub.add_parser("set-password", help="non-interactive password reset")
    p_set.add_argument("--login", required=True)
    p_set.add_argument("--password", help="if omitted, read from stdin")
    p_del = sub.add_parser("delete", help="delete user")
    p_del.add_argument("--login", required=True)
    sub.add_parser("migrate-passwords", help="audit bcrypt migration status")
    args = parser.parse_args()

    if args.cmd is None:
        get_db()
        return main_interactive()
    if args.cmd == "list":
        return cli_list_users(args)
    if args.cmd == "add":
        return addUser()
    if args.cmd == "set-password":
        return cli_set_password(args)
    if args.cmd == "delete":
        return cli_delete_user(args)
    if args.cmd == "migrate-passwords":
        return cli_migrate_passwords(args)
    parser.print_help()


def main_interactive():
    while True:
        try:
            action = getInput(["Show users", "Add user", "Exit"])
        except KeyboardInterrupt:
            print()
            return
        try:
            if action == 0:
                showUsers()
                continue
            elif action == 1:
                addUser()
                continue
            elif action == 2:
                return
        except KeyboardInterrupt:
            print()
            continue

if __name__ == "__main__":
    main()
