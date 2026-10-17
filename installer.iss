; Inno Setup script for the Barangay Queuing and Attendance Management System
; Put this file in the project root (next to build.bat), run build.bat first,
; then open this file in Inno Setup and press Compile (Ctrl+F9).
; Output: installer_output\BarangayQueueSetup.exe

#define MyAppName "Barangay Queuing and Attendance Management"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "Soft-Tech Builders"
#define MyAppExeName "Barangay_Queuing_Attendance_Management.exe"
#define MyBuildDir "dist\Barangay_Queuing_Attendance_Management"

[Setup]
; AppId identifies this app for upgrades/uninstall. Never change it after releasing.
AppId={{1D410D2B-9490-4626-930E-BDDDD9CCD023}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Barangay_Queuing_Attendance_Management
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Terms and conditions page: user must choose "I accept" to continue
LicenseFile=terms_and_conditions.txt
SetupIconFile=app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=installer_output
OutputBaseFilename=Barangay_Queuing_Attendance_Management_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Program Files needs administrator rights to install
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#MyBuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

; NOTE: the database lives in %LOCALAPPDATA%\BarangayQueueManagement (outside {app}),
; so uninstalling or upgrading the program never deletes the barangay's records.
; Do not add an [UninstallDelete] entry for it.
