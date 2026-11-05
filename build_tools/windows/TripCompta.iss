; Installeur Windows de Trip Compta (Inno Setup 6) : fabriqué par build_tools/installeur_windows.py, après
; build_tools/build.py (dossier dist\TripCompta). Résultat : dist\TripCompta-windows-setup.exe.
;
; - Installation pour l'utilisateur seul (pas de droits administrateur) : %LOCALAPPDATA%\Programs\TripCompta,
;   raccourcis menu Démarrer (et Bureau), désinstalleur dans « Applications installées ». Les voyages (Documents\
;   TripCompta ou le dossier choisi) ne sont jamais touchés, ni par l'installation ni par la désinstallation.
; - Fichiers installés par l'installeur : pas de marque « téléchargé depuis Internet » (.NET les charge sans
;   « Débloquer »).
; - WebView2 (la fenêtre de Trip Compta) installé s'il manque (Windows 10 ancien).
; - Mise à jour (lancée par Trip Compta lui-même, compta/updater.py) :
;     TripCompta-windows-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /ATTENDRE=<pid> /ECHEC=<fichier>
;                                  /ANCIEN=<programme actuel>
;   attend la fin de l'application, installe, relance « TripCompta.exe --apres-maj ». En cas d'échec (Inno remet
;   tout en place) : message dans <fichier> et relance de l'ancienne version.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\TripCompta"
#endif
#ifndef OutDir
  #define OutDir "..\..\dist"
#endif

[Setup]
AppId={{6F3C2A91-5B7E-4D2A-9C1E-7A4B8D2E5F10}
AppName=Trip Compta
AppVersion={#AppVersion}
AppVerName=Trip Compta {#AppVersion}
AppPublisher=Trip Compta
AppPublisherURL=https://github.com/Scendari/tripcompta
AppSupportURL=https://github.com/Scendari/tripcompta/blob/main/INSTALLER.md
DefaultDirName={localappdata}\Programs\TripCompta
DefaultGroupName=Trip Compta
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=TripCompta-windows-setup
SetupIconFile=..\icone\TripCompta.ico
UninstallDisplayIcon={app}\TripCompta.exe
UninstallDisplayName=Trip Compta
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
CloseApplications=no
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "bureau"; Description: "Mettre un raccourci sur le Bureau"

[InstallDelete]
; l'ancienne version d'abord : aucun fichier périmé ne reste mélangé à la nouvelle
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist; Check: NeedsWebView2

[Icons]
Name: "{userprograms}\Trip Compta"; Filename: "{app}\TripCompta.exe"
Name: "{userdesktop}\Trip Compta"; Filename: "{app}\TripCompta.exe"; Tasks: bureau

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installation de WebView2 (la fenêtre de Trip Compta)…"; Flags: waituntilterminated skipifdoesntexist; Check: NeedsWebView2
Filename: "{app}\TripCompta.exe"; Description: "Ouvrir Trip Compta"; Flags: nowait postinstall skipifsilent
Filename: "{app}\TripCompta.exe"; Parameters: "--apres-maj"; Flags: nowait; Check: IsUpdate

[Code]
const
  SYNCHRONIZE = $00100000;
  WEBVIEW2 = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function OpenProcess(dwDesiredAccess: Cardinal; bInheritHandle: Boolean; dwProcessId: Cardinal): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: THandle; dwMilliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

var
  Installed: Boolean;

function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:ATTENDRE|}') <> '';
end;

function HasWebView2(Root: Integer; Key: String): Boolean;
var
  V: String;
begin
  Result := RegQueryStringValue(Root, Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

function NeedsWebView2: Boolean;
begin
  Result := not (HasWebView2(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2)
                 or HasWebView2(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2)
                 or HasWebView2(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Pid: Integer;
  H: THandle;
begin
  Result := '';
  Pid := StrToIntDef(ExpandConstant('{param:ATTENDRE|0}'), 0);
  if Pid > 0 then
  begin
    Log(Format('Attente de la fermeture de Trip Compta (%d)', [Pid]));
    H := OpenProcess(SYNCHRONIZE, False, Pid);
    if H <> 0 then
    begin
      if WaitForSingleObject(H, 60000) <> 0 then
        Log('Trip Compta ne s''est pas fermé en 60 s');
      CloseHandle(H);
    end;
    Sleep(1000);   { Windows libère les fichiers un peu après la fin du programme }
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    Installed := True;
end;

procedure DeinitializeSetup;
var
  Msg: TArrayOfString;
  Old: String;
  Rc: Integer;
begin
  if IsUpdate and not Installed then
  begin
    Log('Mise à jour pas installée : relance de l''ancienne version');
    if ExpandConstant('{param:ECHEC|}') <> '' then
    begin
      SetArrayLength(Msg, 1);
      Msg[0] := 'La mise à jour n''a pas pu s''installer. Rien n''a été changé : c''est toujours l''ancienne version.';
      SaveStringsToUTF8File(ExpandConstant('{param:ECHEC|}'), Msg, False);
    end;
    Old := ExpandConstant('{param:ANCIEN|}');
    if (Old <> '') and FileExists(Old) then
      Exec(Old, '--apres-maj', ExtractFileDir(Old), SW_SHOWNORMAL, ewNoWait, Rc);
  end;
end;
