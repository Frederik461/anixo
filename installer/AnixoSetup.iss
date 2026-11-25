; Inno Setup 6 script for ANIXO v0.1.0-beta.3 (Windows only, per-user install).
; Compile from the repository root with:
;   ISCC.exe installer\AnixoSetup.iss
;
; The installer writes only to {app} (%LOCALAPPDATA%\Programs\ANIXO), the
; Start Menu and (optionally) the desktop. The only user-data write is the
; ownership manifest needed by the uninstaller; settings, history, watchlist,
; and progress are never touched.

#define MyAppName "ANIXO"
#define MyAppVersion "0.1.0-beta.3"
#define MyAppPublisher "ANIXO"
#define MyAppExeName "Anixo.exe"

[Setup]
AppId={{41AE1CA4-843F-4365-A741-9FBFFFC8D8B0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\ANIXO
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=no
AllowNoIcons=yes
OutputDir=..\dist\release
OutputBaseFilename=ANIXO-Setup-v{#MyAppVersion}
SetupIconFile=..\assets\anixo-installer.ico
UninstallDisplayIcon={app}\_internal\assets\anixo.ico
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} - A modern anime discovery and streaming application.
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion=0.1.0.0
VersionInfoVersion=0.1.0.0
VersionInfoCopyright=Copyright (C) 2026 ANIXO

; Optional branded assets: included only when present in the repository.
#if FileExists("..\assets\anixo-wizard.bmp")
WizardImageFile=..\assets\anixo-wizard.bmp
#endif
#if FileExists("..\assets\anixo-wizard-small.bmp")
WizardSmallImageFile=..\assets\anixo-wizard-small.bmp
#endif
#if FileExists("..\LICENSE.txt")
LicenseFile=..\LICENSE.txt
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; One-folder PyInstaller output. User data directories are deliberately not
; referenced here, so uninstalling never removes settings or history.
Source: "..\dist\Anixo\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\ANIXO-Uninstaller.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\packaging\uninstall_windows.cmd"; DestName: "anixo-uninstall.cmd"; DestDir: "{app}"; Flags: ignoreversion

[Run]
; Create the persistent ownership receipt during install, before the app can
; be launched. This writes only the manifest JSON in the user data directory.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--record-install --install-dir ""{app}"" --install-kind windows-installer"; Flags: waituntilterminated runhidden

[UninstallRun]
; Remove ANIXO-managed dependencies and caches via the same guarded manifest
; logic used by the in-app uninstaller. The wrapper replays install-time data
; path overrides. OS-level uninstall keeps settings and personal data.
Filename: "{cmd}"; Parameters: "/c ""{app}\anixo-uninstall.cmd"""; Flags: waituntilterminated skipifdoesntexist; RunOnceId: AnixoCleanup

[Icons]
; {group} honours /NOICONS while still creating the normal Start Menu folder.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\_internal\assets\anixo.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\_internal\assets\anixo.ico"; Tasks: desktopicon

[UninstallDelete]
; Written dynamically by --record-install, so it is not in Inno's file list.
; The wrapper normally deletes it earlier; this is a fallback for a failed
; wrapper run. Removing the empty root folder is safe: dirifempty never
; deletes an app directory that still contains user-created files.
Type: files; Name: "{app}\uninstall-paths.ini"
Type: dirifempty; Name: "{app}"

[Messages]
SetupWindowTitle=Install {#MyAppName} v{#MyAppVersion}
