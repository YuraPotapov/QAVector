; Inno Setup script for QAVector.
;
; Not run by hand - packaging\build_exe.ps1 passes the version and the repo root:
;   ISCC.exe /DAppVersion=0.8.1 /DRepoRoot=C:\src\cms /Oinstallers\windows\0.8.1 installer.iss
;
; The Windows counterpart of packaging/linux/control.in and the .desktop file:
; where the .deb drops two bundles under /opt and wires up /usr/bin wrappers and
; a menu entry, this drops the same two bundles under Program Files and wires up
; Start-menu shortcuts and an optional PATH entry.

#ifndef AppVersion
  #error Pass /DAppVersion=<version> (build_exe.ps1 does this)
#endif
#ifndef RepoRoot
  #error Pass /DRepoRoot=<path to the checkout> (build_exe.ps1 does this)
#endif

#define AppName      "QAVector"
#define AppPublisher "Yurii Potapov"
#define AppExeName   "qavector-gui.exe"
#define CoreExeName  "qavector-core.exe"

[Setup]
; Kept from when this was Chrome Multi Session: the id is what makes a new
; version an upgrade of that install rather than a second program beside it.
AppId={{7C1F5B84-3E2A-4D6C-9A21-5E0B7D4C8F13}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\QAVector
DefaultGroupName={#AppName}
OutputBaseFilename=qavector-{#AppVersion}-setup
SetupIconFile={#RepoRoot}\build\icons\icon.ico
UninstallDisplayIcon={app}\gui\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The bundles are 64-bit; without this they would land in Program Files (x86).
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Per-machine when elevated, per-user when not: a QA machine is often locked
; down, and needing an administrator to try the tool is a reason not to try it.
PrivilegesRequiredOverridesAllowed=dialog
; An upgrade installs over the old copy rather than beside it, and keeps
; the project folder the user picked last time (see DataDirPage).
UsePreviousAppDir=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "Add the command-line tool to PATH"; \
    GroupDescription: "Command line"; Flags: unchecked

[Files]
; Both PyInstaller onedir bundles, whole. The GUI finds the core next to it at
; runtime (cms_gui.core.frozen_core), which is why the layout under {app} has to
; match what the .deb puts under /opt/qavector.
Source: "{#RepoRoot}\build\dist\core\*"; DestDir: "{app}\core"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\build\dist\gui\*";  DestDir: "{app}\gui"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\build\VERSION";     DestDir: "{app}"; Flags: ignoreversion
; The icon as a file as well as a resource inside the two .exe files. The
; shortcuts below name it directly: Windows caches a shortcut's icon, and an
; upgrade that only changes what is embedded in the executable can leave the
; old one sitting on the desktop.
Source: "{#RepoRoot}\build\icons\icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; The user's own directory, made at install time so the app never has to ask
; where to put its first report. uninsneveruninstall is the whole contract of
; this folder: scenarios, reports, sessions and credentials outlive the program.
Name: "{code:GetDataDir}"; Flags: uninsneveruninstall

[INI]
; How the installer tells the app where that folder is. Read by
; runtime_paths.configured_data_root() in the core and cms_gui.core in the GUI.
Filename: "{app}\cms.ini"; Section: "Paths"; Key: "data_dir"; \
    String: "{code:GetDataDir}"; Flags: createkeyifdoesntexist uninsdeleteentry

[Icons]
Name: "{group}\{#AppName}";       Filename: "{app}\gui\{#AppExeName}"; \
    IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\gui\{#AppExeName}"; \
    IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Registry]
; Opt-in, and on the user's own PATH rather than the machine's, so it works
; without an administrator and uninstalls cleanly.
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}\core"; Tasks: addtopath; \
    Check: NeedsAddPath('{app}\core')

[Run]
Filename: "{app}\gui\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent

[Code]
var
  DataDirPage: TInputDirWizardPage;

// The project folder: where scenarios, reports, browser sessions and users.json
// live. Deliberately its own question rather than a subfolder of the install
// directory - the program is replaced wholesale on upgrade and sits in Program
// Files, and none of that is true of the things the user makes.
procedure InitializeWizard();
begin
  DataDirPage := CreateInputDirPage(wpSelectDir,
    'Select Project Folder',
    'Where should QAVector keep your work?',
    'Your accounts (users.json), the scenarios you record or edit, run reports' + #13#10 +
    'and screenshots, and one Chrome profile per test user are all stored here.' + #13#10#13#10 +
    'This folder is yours: installing, upgrading and uninstalling never touch' + #13#10 +
    'its contents. Pick any location you like, then click Next.',
    False, '');
  DataDirPage.Add('');
  // An upgrade offers the folder already in use; a first install offers the
  // default the app would have picked for itself.
  DataDirPage.Values[0] := GetPreviousData('DataDir',
    ExpandConstant('{%USERPROFILE}\QAVector'));
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'DataDir', DataDirPage.Values[0]);
end;

function GetDataDir(Param: string): string;
begin
  Result := DataDirPage.Values[0];
end;

// Named on the final page, because it is the answer to "where did my reports
// go?" and nobody reads an installer log to find out.
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo,
  MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine +
            'Project folder (your scenarios, reports and sessions):' + NewLine +
            Space + GetDataDir('') + NewLine;
  if MemoGroupInfo <> '' then
    Result := Result + NewLine + MemoGroupInfo + NewLine;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + MemoTasksInfo + NewLine;
end;

// True when this directory is not already on the user's PATH. Without the
// check, repeated installs append the same entry until PATH stops working.
function NeedsAddPath(Param: string): Boolean;
var
  Existing: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Existing) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(ExpandConstant(Param)) + ';',
                ';' + Uppercase(Existing) + ';') = 0;
end;

// Chrome is the one thing this cannot ship and cannot work without. Said at
// install time, where it is actionable, rather than at first launch.
function InitializeSetup(): Boolean;
var
  Dummy: string;
begin
  Result := True;
  if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', '', Dummy) then
    exit;
  if RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', '', Dummy) then
    exit;
  if MsgBox('Google Chrome was not found on this computer.' + #13#10#13#10 +
            'QAVector drives Chrome; it does not include one. ' +
            'Install it from https://www.google.com/chrome/ and it will be ' +
            'found automatically.' + #13#10#13#10 + 'Continue anyway?',
            mbConfirmation, MB_YESNO) = IDNO then
    Result := False;
end;
