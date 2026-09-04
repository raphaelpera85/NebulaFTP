; ==============================================
; MulletaFlix Inno Setup Script
; Creates a professional Windows installer
; ==============================================

#define AppName "MulletaFlix"
#define AppVersion "1.0.0"
#define AppPublisher "MulletaFlix Team"
#define AppURL "https://github.com/mulletaflix"
#define AppExeName "MulletaFlix.exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
OutputDir=Output
OutputBaseFilename=MulletaFlix_Setup
Compression=lzma/ultra64
SolidCompression=yes
WizardStyle=modern
SetupIconFile=img\mulletaflix.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
UninstallLogMode=append
CreateAppDir=yes
DisableDirPage=no
DisableProgramGroupPage=no
LicenseFile=LICENSE
InfoBeforeFile=README.md
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "portuguese"; MessagesFile: "compiler:Languages\Portuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startmenuicon"; Description: "{cm:CreateStartMenuIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checked
Name: "pinstart"; Description: "Pin to Start Menu"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "pintaskbar"; Description: "Pin to Taskbar"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "firewall"; Description: "Add firewall rule for FTP/HTTP ports"; GroupDescription: "Network"; Flags: unchecked
Name: "rclone"; Description: "Configure rclone mount (requires rclone installed)"; GroupDescription: "Network"; Flags: unchecked

[Files]
; Main executable
Source: "dist\MulletaFlix.exe"; DestDir: "{app}"; Flags: ignoreversion

; Configuration template
Source: ".env.mulletaflix"; DestDir: "{app}"; Flags: ignoreversion; DestName: ".env.template"

; Documentation
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "MULTI_INSTANCE.md"; DestDir: "{app}"; Flags: ignoreversion

; Tools
Source: "tools\bootstrap.py"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "tools\clean_already_sent.py"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "tools\feed_ftp.py"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "tools\start_rclone_z.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "tools\supabase_sync.py"; DestDir: "{app}\tools"; Flags: ignoreversion

; Scripts
Source: "start-mulletaflix.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "start-original.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "start-both.bat"; DestDir: "{app}"; Flags: ignoreversion

; FTP module
Source: "ftp\*"; DestDir: "{app}\ftp"; Flags: ignoreversion recursesubdirs

; Requirements
Source: "requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "requirements.in"; DestDir: "{app}"; Flags: ignoreversion
Source: "pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion

; Icons
Source: "img\*"; DestDir: "{app}\img"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartmenu}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: startmenuicon

[Run]
; Post-install: copy .env.template to .env if .env doesn't exist
Filename: "{cmd}"; Parameters: "/c if not exist ""{app}\.env"" copy ""{app}\.env.template"" ""{app}\.env"""; StatusMsg: "Creating configuration file..."; Flags: runhidden shellexec waituntilterminated

; Post-install: run bootstrap to verify dependencies
Filename: "{app}\python.exe"; Parameters: "{app}\tools\bootstrap.py"; WorkingDir: "{app}"; StatusMsg: "Verifying Python dependencies..."; Flags: runhidden waituntilterminated

[Registry]
; Add firewall rules if task selected
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules"; ValueType: string; ValueName: "MulletaFlix-FTP"; ValueData: "v2.30|Action=Allow|Active=TRUE|Dir=In|Protocol=6|LPort=2123|Name=MulletaFlix FTP Server"; Flags: uninsdeletevalue; Tasks: firewall
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules"; ValueType: string; ValueName: "MulletaFlix-FTP-Passive"; ValueData: "v2.30|Action=Allow|Active=TRUE|Dir=In|Protocol=6|LPort=60010-60019|Name=MulletaFlix FTP Passive"; Flags: uninsdeletevalue; Tasks: firewall
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules"; ValueType: string; ValueName: "MulletaFlix-Control"; ValueData: "v2.30|Action=Allow|Active=TRUE|Dir=In|Protocol=6|LPort=2131|Name=MulletaFlix Control Plane"; Flags: uninsdeletevalue; Tasks: firewall
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules"; ValueType: string; ValueName: "MulletaFlix-Stream"; ValueData: "v2.30|Action=Allow|Active=TRUE|Dir=In|Protocol=6|LPort=2124|Name=MulletaFlix HTTP Stream"; Flags: uninsdeletevalue; Tasks: firewall

; Add to Windows Startup if needed (optional)
; Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "MulletaFlix"; ValueData: """{app}\{#AppExeName}"""; Flags: uninsdeletevalue

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Custom functions for advanced setup
function InitializeSetup(): Boolean;
begin
  // Check if running as administrator
  if not IsAdminLoggedOn() then
  begin
    MsgBox('This installer requires administrator privileges. Please run as administrator.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
end;

procedure CurStepChanged(CurStep: Integer);
begin
  if CurStep = ssPostInstall then
  begin
    // Post-install actions
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
end;