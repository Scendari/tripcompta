"""ESSAI (branche essai-msix) : Trip Compta en MSIX, pour de vrai, sur Windows (GitHub Actions).

    python build_tools/msix/essai_msix.py 1.4.0-dev

Après build_tools/build.py : fabrique le .msix (makeappx), le signe avec un certificat d'essai, l'installe, puis
mesure ce qui change dans le conteneur MSIX. Rien n'est publié. Résultat : msix-rapport.json (et le journal).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "build_tools"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
R = {}


def note(k, v):
    R[k] = v
    print(f"{k} : {v}", flush=True)


def ps(cmd, check=True):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    if check and r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit(f"PowerShell : {cmd}")
    return r.stdout.strip()


def sdk_tool(name):
    kits = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin"
    found = sorted(kits.glob(f"10.*/x64/{name}"))
    if not found:
        raise SystemExit(f"{name} introuvable (Windows SDK)")
    return str(found[-1])


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ping(port, secs=60):
    for _ in range(secs * 2):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
                return json.load(r)
        except Exception:
            time.sleep(0.5)
    return None


def quit_app(port):
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/quitter", data=b"", method="POST"),
                               timeout=5)
    except Exception:
        pass
    time.sleep(2)


def main():
    version = sys.argv[1]
    work = Path(tempfile.mkdtemp(prefix="msix-"))
    layout = work / "layout"
    shutil.copytree(ROOT / "dist" / "TripCompta", layout)
    (layout / "Assets").mkdir()
    from PIL import Image
    icon = Image.open(ROOT / "build_tools" / "icone" / "TripCompta.png").convert("RGBA")
    for name, size in (("StoreLogo", 50), ("Square44x44Logo", 44), ("Square150x150Logo", 150)):
        icon.resize((size, size), Image.LANCZOS).save(layout / "Assets" / f"{name}.png")
    num = version.split("-")[0] + ".0"
    (layout / "AppxManifest.xml").write_text(
        (HERE / "AppxManifest.xml").read_text(encoding="utf-8").replace("{VERSION}", num), encoding="utf-8")
    msix = work / "TripCompta.msix"
    t0 = time.time()
    r = subprocess.run([sdk_tool("makeappx.exe"), "pack", "/d", str(layout), "/p", str(msix), "/o"],
                       capture_output=True, text=True)
    note("makeappx", "ok" if r.returncode == 0 else r.stdout[-2000:] + r.stderr[-2000:])
    if r.returncode:
        raise SystemExit(1)
    note("taille_msix_mo", round(msix.stat().st_size / 1e6, 1))
    note("duree_emballage_s", round(time.time() - t0))

    # certificat d'essai (le Store, lui, re-signe : rien de tout ça pour une publication)
    pfx, pw = work / "essai.pfx", "essai-msix"
    ps(f"$c = New-SelfSignedCertificate -Type Custom -Subject 'CN=TripComptaEssai' -KeyUsage DigitalSignature "
       f"-FriendlyName 'Trip Compta essai' -CertStoreLocation Cert:\\CurrentUser\\My "
       f"-TextExtension @('2.5.29.37={{text}}1.3.6.1.5.5.7.3.3', '2.5.29.19={{text}}'); "
       f"$p = ConvertTo-SecureString -String '{pw}' -Force -AsPlainText; "
       f"Export-PfxCertificate -Cert $c -FilePath '{pfx}' -Password $p | Out-Null; "
       f"Import-PfxCertificate -FilePath '{pfx}' -CertStoreLocation Cert:\\LocalMachine\\TrustedPeople -Password $p | Out-Null")
    r = subprocess.run([sdk_tool("signtool.exe"), "sign", "/fd", "SHA256", "/f", str(pfx), "/p", pw, str(msix)],
                       capture_output=True, text=True)
    note("signature_essai", "ok" if r.returncode == 0 else r.stdout + r.stderr)
    ps(f"Add-AppxPackage -Path '{msix}'")
    pfn = ps("(Get-AppxPackage -Name TripCompta.Essai).PackageFamilyName")
    loc = ps("(Get-AppxPackage -Name TripCompta.Essai).InstallLocation")
    note("installe", bool(pfn))
    note("dossier_installation", loc)
    alias = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WindowsApps" / "tripcompta.exe"
    note("alias_present", alias.exists())
    note("alias_dans_PATH", any(Path(p) == alias.parent for p in os.environ.get("PATH", "").split(os.pathsep) if p))

    # 1. sondes dans le conteneur
    out = work / "diag.json"
    subprocess.run([str(alias), "--diagnostic-msix", str(out)], timeout=120)
    d = json.loads(out.read_text(encoding="utf-8"))
    note("diag_dans_paquet", d)
    local_cache = Path(os.environ["LOCALAPPDATA"]) / "Packages" / pfn / "LocalCache"
    real = {"appdata": Path(os.environ["APPDATA"]), "localappdata": Path(os.environ["LOCALAPPDATA"]),
            "documents": Path(d["documents"]), "temp": Path(tempfile.gettempdir())}
    where = {}
    for k, base in real.items():
        visible = (base / "TripComptaSonde" / "sonde.txt").exists()
        cached = [str(p) for p in local_cache.rglob("TripComptaSonde")] if local_cache.exists() else []
        where[k] = {"visible_hors_paquet": visible, "dans_LocalCache": [c for c in cached if k in c.lower()
                                                                          or k.startswith("app") or k == "temp"]}
    note("ou_atterrissent_les_fichiers", where)
    note("LocalCache_contenu", sorted(str(p.relative_to(local_cache)) for p in local_cache.rglob("*"))[:40]
         if local_cache.exists() else None)
    import keyring
    note("trousseau_visible_hors_paquet", keyring.get_password("Trip Compta essai MSIX", "sonde") == "secret-msix")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\TripComptaSonde") as k:
            note("registre_visible_hors_paquet", winreg.QueryValueEx(k, "sonde")[0] == "registre")
    except OSError:
        note("registre_visible_hors_paquet", False)

    # 2. l'application elle-même, par l'alias, réglages et dossier par défaut (comme un vrai utilisateur)
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("TRIPCOMPTA_")}
    env["TRIPCOMPTA_NO_UPDATE_CHECK"] = "1"
    p = subprocess.Popen([str(alias), "--navigateur", "--no-browser", "--port", str(port)], env=env)
    info = wait_ping(port)
    note("app_par_alias_repond", info)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/voyages", method="POST",
                                     data=json.dumps({"nom": "Voyage MSIX"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            note("api_ecriture", r.status)
    except Exception as e:
        note("api_ecriture", repr(e))
    quit_app(port)
    p.wait(30)
    docs_db = Path(d["dossier_voyages"]) / "compta.db"
    note("base_dans_Documents_visible_hors_paquet", docs_db.exists())
    note("reglages_visibles_hors_paquet", Path(d["reglages"]).exists())
    note("reglages_dans_LocalCache", [str(p) for p in local_cache.rglob("reglages.json")] if local_cache.exists() else [])
    note("verrous_instance_dans_LocalCache", [p.name for p in local_cache.rglob("instance-*")] if local_cache.exists() else [])

    # 3. smoke complet (protection, trousseau) et VRAIE fenêtre WebView2, par l'alias
    import smoke
    note("smoke_protection_par_alias", smoke.main(str(alias), version) == 0)
    note("smoke_fenetre_webview2_par_alias", smoke.window(str(alias), version) == 0)

    # 4. IA : le serveur MCP lancé par l'alias (comme le ferait Claude Desktop), en stdio
    data_mcp = work / "voyages mcp"
    m = subprocess.Popen([str(alias), "--mcp", "--data-dir", str(data_mcp)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "essai", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    try:
        out_, err_ = m.communicate("".join(json.dumps(x) + "\n" for x in msgs).encode(), timeout=60)
    except subprocess.TimeoutExpired:
        m.kill()
        out_, err_ = m.communicate()
    tools = [json.loads(line) for line in out_.decode("utf-8", "replace").splitlines() if line.strip().startswith("{")]
    names = [t["name"] for x in tools if x.get("id") == 2 for t in x.get("result", {}).get("tools", [])]
    note("mcp_par_alias_outils", names[:8])
    note("mcp_par_alias_ok", "lister_voyages" in names)
    note("mcp_stderr", err_.decode("utf-8", "replace")[-600:])

    # 5. désinstallation : les voyages restent, le conteneur disparaît
    ps("Get-AppxPackage -Name TripCompta.Essai | Remove-AppxPackage")
    time.sleep(3)
    note("apres_desinstallation_base_Documents_presente", docs_db.exists())
    note("apres_desinstallation_LocalCache_present", local_cache.exists())
    note("apres_desinstallation_alias_present", alias.exists())
    (ROOT / "msix-rapport.json").write_text(json.dumps(R, indent=1, ensure_ascii=False, default=str),
                                            encoding="utf-8")
    print("Rapport : msix-rapport.json")


if __name__ == "__main__":
    main()
