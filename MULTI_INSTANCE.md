# Running Multiple NebulaFTP Instances Simultaneously

This guide explains how to run the **Original Nebula** and **MulletaFlix** instances at the same time on the same machine.

## Port Allocation

| Service | Original Nebula | MulletaFlix |
|---------|----------------|-------------|
| FTP Server | 2121 | **2123** |
| FTP Passive Ports | 60000-60009 | **60010-60019** |
| Control Plane (HTTP API) | 2130 | **2131** |
| HTTP Stream | 2122 | **2124** |
| MongoDB Database | `ftp` | **`ftp_mulletaflix`** |
| STRM Output Dir | `strm_library` | **`strm_library_mulletaflix`** |
| Feeder State Dir | `state` | **`state_mulletaflix`** |

## Quick Start

### Option 1: Automated (Both at Once)
```cmd
start-both.bat
```
This script starts both instances in sequence, using the correct `.env` for each.

### Option 2: Manual (Separate Windows)

**Terminal 1 - Original Nebula:**
```cmd
start-original.bat
```

**Terminal 2 - MulletaFlix:**
```cmd
start-mulletaflix.bat
```

## Configuration Files

### Original Nebula (`.env`)
Uses default ports. Copy from `.env.example` and fill in your values:
```env
PORT=2121
PASSIVE_PORTS=60000-60009
CONTROL_PORT=2130
STREAM_PORT=2122
MONGO_DATABASE=ftp
STRM_OUTPUT_DIR=strm_library
FEED_STATE_DIR=state
```

### MulletaFlix (`.env.mulletaflix`)
Pre-configured with alternate ports. Copy to `.env` when running MulletaFlix:
```env
PORT=2123
PASSIVE_PORTS=60010-60019
CONTROL_PORT=2131
STREAM_PORT=2124
MONGO_DATABASE=ftp_mulletaflix
STRM_OUTPUT_DIR=strm_library_mulletaflix
FEED_STATE_DIR=state_mulletaflix
```

## Important Notes

### 1. MongoDB
Both instances can share the same MongoDB server (`mongodb://localhost:27017`) but **must use different database names** (`ftp` vs `ftp_mulletaflix`) to avoid data conflicts.

### 2. Telegram Bots
Each instance needs its own set of bot tokens (`BOT_TOKENS`) and chat IDs (`CHAT_ID`, `BACKUP_CHAT_ID`). Using the same bots for both instances will cause message conflicts.

### 3. Monitored Directories
Use different source directories for each instance (`FEED_ALLOWED_ROOTS`) to avoid processing the same files twice.

### 4. Firewall
If running on Windows, allow both port ranges in Windows Firewall:
- Original: 2121, 2130, 2122, 60000-60009
- MulletaFlix: 2123, 2131, 2124, 60010-60019

### 5. rclone Mount
The `start_rclone_z.ps1` script mounts a drive letter (default Z:). Only one instance should run this, or configure different mount points/drive letters for each.

## Stopping Instances

Close the terminal windows where each instance is running, or use Task Manager to end the `python.exe` processes.

## Troubleshooting

### "Address already in use"
Another process is using one of the ports. Check with:
```cmd
netstat -ano | findstr :2121
netstat -ano | findstr :2123
```
Kill the conflicting process or change the port in the respective `.env` file.

### MongoDB Connection Issues
Ensure MongoDB is running on `localhost:27017` and both databases (`ftp` and `ftp_mulletaflix`) exist.

### File Conflicts
If both instances write to the same STRM output directory, they will overwrite each other's `.strm` files. The separate `STRM_OUTPUT_DIR` settings prevent this.

## Scripts Summary

| Script | Purpose |
|--------|---------|
| `start-original.bat` | Starts Original Nebula using `.env` |
| `start-mulletaflix.bat` | Starts MulletaFlix using `.env.mulletaflix` |
| `start-both.bat` | Starts both instances sequentially |
| `start-nebula.bat` | Legacy script (uses `.env`) |
| `start-gui.bat` | Starts GUI version (uses `.env`) |

## Customizing Ports Further

To use different ports, edit the respective `.env` file:
```env
# FTP Server
PORT=2125
PASSIVE_PORTS=60020-60029

# Control Plane
CONTROL_PORT=2132

# HTTP Stream
STREAM_PORT=2126

# Database
MONGO_DATABASE=ftp_custom
```

Then update the corresponding startup script or use `start-both.bat` with your custom configs.