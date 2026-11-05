"""ESSAI (branche essai-msix) : ce que voit Trip Compta dans un conteneur MSIX. « TripCompta.exe --diagnostic-msix
SORTIE.json » écrit des sondes partout où l'app écrit d'habitude, et note ce qu'il voit (identité du paquet…)."""
import json
import os
import socket
import sys
import tempfile
from pathlib import Path


def package_full_name():
    import ctypes
    n = ctypes.c_uint32(0)
    rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(n), None)
    if rc != 122:                                     # 15700 : pas dans un paquet
        return None, rc
    buf = ctypes.create_unicode_buffer(n.value)
    ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(n), buf)
    return buf.value, 0


def run(out: str):
    d = {}
    d["paquet"], d["paquet_rc"] = package_full_name()
    d["exe"] = sys.executable
    from compta import paths
    d["reglages"] = str(paths.config_file())
    d["documents"] = str(paths.documents_dir())
    d["dossier_voyages"] = str(paths.default_data_dir())
    probes = {"appdata": Path(os.environ["APPDATA"]), "localappdata": Path(os.environ["LOCALAPPDATA"]),
              "documents": paths.documents_dir(), "temp": Path(tempfile.gettempdir())}
    d["sondes"] = {}
    for k, base in probes.items():
        f = base / "TripComptaSonde" / "sonde.txt"
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("sonde " + k, encoding="utf-8")
            d["sondes"][k] = {"chemin": str(f), "relu": f.read_text(encoding="utf-8") == "sonde " + k}
        except Exception as e:
            d["sondes"][k] = {"chemin": str(f), "erreur": repr(e)}
    try:
        import keyring
        keyring.set_password("Trip Compta essai MSIX", "sonde", "secret-msix")
        d["trousseau"] = keyring.get_password("Trip Compta essai MSIX", "sonde") == "secret-msix"
    except Exception as e:
        d["trousseau"] = repr(e)
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\TripComptaSonde") as k:
            winreg.SetValueEx(k, "sonde", 0, winreg.REG_SZ, "registre")
        d["registre"] = True
    except Exception as e:
        d["registre"] = repr(e)
    try:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        c = socket.create_connection(srv.getsockname(), timeout=3)
        c.close()
        srv.close()
        d["boucle_locale"] = True
    except Exception as e:
        d["boucle_locale"] = repr(e)
    try:
        from compta import fenetre
        d["edge"] = fenetre.edge_path()
    except Exception as e:
        d["edge"] = repr(e)
    Path(out).write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
