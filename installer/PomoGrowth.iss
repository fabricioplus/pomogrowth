; Instalador de PomoGrowth (Inno Setup 6.6 o posterior: colores #rrggbb).
;
; Pasos, desde la raíz del repositorio:
;   1. python scripts/build_brand_assets.py  (solo si cambia la mascota:
;      assets/icon.ico y las imágenes del asistente, installer/*.bmp)
;   2. python -m PyInstaller PomoGrowth.spec --noconfirm --distpath build/release --workpath build/work
;      Un build limpio, sin datos: en dist/ puede haber una instalación con los del usuario.
;   3. ISCC.exe installer\PomoGrowth.iss  ->  dist\PomoGrowth_v<versión>_instalador.exe
;
; Instalación por usuario y sin administrador, en %LOCALAPPDATA%\Programs\PomoGrowth:
; la app guarda sus datos (JSON, copias, logs y la música de la carpeta lofi)
; JUNTO al exe, y en Program Files no podría escribirlos. Por eso tampoco se
; ofrece instalar para todos los usuarios.

#define AppName "PomoGrowth"
; La misma que Config.APP_VERSION (core/config.py): lo comprueba tests/test_installer.py
#define AppVersion "1.0"
#define AppExe "PomoGrowth.exe"
; Hasta la 9.4 (versiones internas, antes de la 1.0) el exe se llamaba así
#define OldAppExe "PomoGrowth9.exe"
; El mismo que Config.APP_USER_MODEL_ID: con él en los accesos directos, la
; barra de tareas agrupa la ventana con el acceso anclado y muestra su icono
#define AppUserModelID "PomoGrowth.App.v9"
; El mismo que Config.APP_MUTEX: la app lo crea mientras está abierta y el
; desinstalador lo busca para pedir que se cierre antes ([Code])
#define AppMutex "PomoGrowth.App.Running"
; El build que se empaqueta (paso 2). Su exe lleva la versión de la app con
; cuatro números (PomoGrowth.spec completa con ceros: 1.0 -> 1.0.0.0 y
; 1.0.1 -> 1.0.1.0): si falta o es otra, el build es viejo (los de antes de la
; 9.3.1 no llevan versión) y el instalador no se compila
#define BuildDir "..\build\release\PomoGrowth"
#define BuildVersion GetVersionNumbersString(AddBackslash(AddBackslash(SourcePath) + BuildDir) + AppExe)
#if BuildVersion != AppVersion + ".0" && BuildVersion != AppVersion + ".0.0"
  #pragma message "Versión del exe del build: " + (BuildVersion == "" ? "ninguna" : BuildVersion) + "; la del instalador: " + AppVersion
  #error No hay un build de esta versión en build\release: compilarlo (paso 2) antes del instalador
#endif

[Setup]
; Identifica la instalación al actualizar y desinstalar: no cambiarlo nunca
AppId={{2F31BE25-5AD3-47EA-8D58-687FDFC26D86}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Fabricio S.
AppCopyright=© 2026 Fabricio S.
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Instalador de {#AppName}
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
; La bienvenida muestra la marca (WizardImageFile); Inno 6 la oculta por defecto
DisableWelcomePage=no
; Qt 6 necesita Windows 10 1809 o posterior, de 64 bits
MinVersion=10.0.17763
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Al actualizar, Setup ofrece cerrar PomoGrowth si está abierto (también
; desde la bandeja) y no lo vuelve a abrir. El desinstalador no tiene esa
; opción: [Code] pide cerrarlo antes (mutex {#AppMutex})
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=..\assets\icon.ico
WizardStyle=modern
; Marca oficial, generadas por scripts/build_brand_assets.py: una imagen por
; escala de pantalla (100 a 250 %). Los colores de fondo rellenan el hueco
; que quede alrededor al mantener la proporción
WizardImageFile=wizard_164x314.bmp,wizard_192x386.bmp,wizard_246x459.bmp,wizard_273x556.bmp,wizard_328x604.bmp,wizard_355x700.bmp,wizard_410x797.bmp
WizardSmallImageFile=wizard_small_55x55.bmp,wizard_small_64x68.bmp,wizard_small_83x80.bmp,wizard_small_92x97.bmp,wizard_small_110x106.bmp,wizard_small_119x123.bmp,wizard_small_138x140.bmp
WizardImageBackColor=#0F1720
WizardSmallImageBackColor=#FFFFFF
OutputDir=..\dist
OutputBaseFilename=PomoGrowth_v{#AppVersion}_instalador
Compression=lzma2/ultra64
SolidCompression=yes

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Messages]
FinishedLabel=El programa completó la instalación de [name] en su sistema.%n%nSus datos (tareas, progreso y ajustes) se guardan en la carpeta de instalación, junto al programa: se conservan al actualizar y, al desinstalar, se pregunta antes de borrarlos.%n%nEl modo enfoque bloquea sitios en el archivo hosts de Windows: para usarlo, abra [name] como administrador.
FinishedLabelNoIcons=El programa completó la instalación de [name] en su sistema.%n%nSus datos (tareas, progreso y ajustes) se guardan en la carpeta de instalación, junto al programa: se conservan al actualizar y, al desinstalar, se pregunta antes de borrarlos.%n%nEl modo enfoque bloquea sitios en el archivo hosts de Windows: para usarlo, abra [name] como administrador.

[CustomMessages]
BorrarDatos={#AppName} se ha desinstalado, pero sus datos siguen en la carpeta de instalación: las tareas, el progreso y los ajustes (con el token de Asana y las claves de IA), sus copias de seguridad y los registros.%n%n¿Desea borrarlos también? Si los conserva, al volver a instalar en la misma carpeta se recuperan.%n%nLa música de la carpeta lofi y lo exportado no se borran.
CerrarAntes={#AppName} está abierto. Ciérrelo (con la X de su ventana o con «Cerrar» en el icono de la bandeja) y pulse Aceptar para continuar con la desinstalación.
ExeViejo=No se pudo quitar {#OldAppExe}, el programa de la versión anterior: seguramente seguía abierto.%n%nCierre {#AppName} (también si lo abrió como administrador) y vuelva a ejecutar este instalador.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; El _internal anterior entero: PyInstaller cambia archivos entre builds y lo
; viejo se mezclaría con lo nuevo. Ahí solo hay recursos, nunca datos
Type: filesandordirs; Name: "{app}\_internal"
; Al actualizar desde la 9.4 o anterior, su exe no debe quedarse junto al nuevo
; (los accesos directos ya apuntan a {#AppExe})
Type: files; Name: "{app}\{#OldAppExe}"

[Files]
; Nunca datos del usuario: si se abrió el exe del build para probarlo, junto a
; él quedan sus JSON (con el token de Asana), copias, logs, exportaciones y música
Source: "{#BuildDir}\*"; DestDir: "{app}"; Excludes: "\*.json,\*.bak,\*.tmp,\*.corrupt-*,\*.csv,\*.xlsx,\logs,\assets"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\assets\music\lofi\LEEME.txt"; DestDir: "{app}\assets\music\lofi"; Flags: onlyifdoesntexist
; La GPL pide entregar su texto con el programa (.txt: se abre con doble clic)
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; AppUserModelID: "{#AppUserModelID}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; AppUserModelID: "{#AppUserModelID}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// Desinstalar con la app abierta dejaría su exe y sus DLL (el desinstalador
// no usa Restart Manager, a diferencia de Setup) y, si además se borran los
// datos, la app los volvería a escribir. Mientras está abierta crea el mutex
// {#AppMutex} (Config.APP_MUTEX): se pide cerrarla antes. Abierta como
// administrador puede no verse; en silencio no se pregunta
function InitializeUninstall(): Boolean;
begin
  Result := True;
  while not UninstallSilent and CheckForMutexes('{#AppMutex}') do
    if MsgBox(CustomMessage('CerrarAntes'), mbError, MB_OKCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
end;

// Al actualizar desde la 9.4 el exe viejo se borra en [InstallDelete], que no
// avisa si no puede (seguía abierto): aquí sí, para no dejarlo junto al nuevo
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and FileExists(ExpandConstant('{app}\{#OldAppExe}')) then
  begin
    Log('No se pudo borrar {#OldAppExe}');
    if not WizardSilent then
      MsgBox(CustomMessage('ExeViejo'), mbError, MB_OK);
  end;
end;

// Los datos del usuario viven en {app}, junto al exe (utils/paths.app_base), y
// el instalador no los registra: desinstalar no los toca. Al terminar se ofrece
// borrarlos; por defecto se conservan, y en una desinstalación silenciosa,
// siempre. La música de la carpeta lofi y lo exportado no se borran nunca.

function HayDatos(const App: String): Boolean;
begin
  Result := FileExists(App + '\pomogrowth_data.json') or
    FileExists(App + '\user_progress.json') or
    FileExists(App + '\focus_config.json') or DirExists(App + '\logs');
end;

procedure BorrarDatos(const App: String);
begin
  DeleteFile(App + '\pomogrowth_data.json');
  DeleteFile(App + '\user_progress.json');
  DeleteFile(App + '\focus_config.json');
  // Copias (.bak), temporales y corruptos preservados (utils/safe_json.py), y
  // las copias previas a importar o a borrar todo desde Ajustes
  DelTree(App + '\*.bak', False, True, False);
  DelTree(App + '\*.tmp', False, True, False);
  DelTree(App + '\*.corrupt-*', False, True, False);
  DelTree(App + '\pomogrowth_data.pre-*.json', False, True, False);
  DelTree(App + '\logs', True, True, True);
  // Las carpetas, solo si se han quedado vacías (sin música ni exportaciones)
  RemoveDir(App + '\assets\music\lofi');
  RemoveDir(App + '\assets\music');
  RemoveDir(App + '\assets');
  RemoveDir(App);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  App: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  App := ExpandConstant('{app}');
  if HayDatos(App) and not UninstallSilent and
     (MsgBox(CustomMessage('BorrarDatos'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
    BorrarDatos(App);
end;
