"""
Radar de précipitations MétéoSuisse → calque PNG pour la carte 3D de la Bibliothèque des Sales Gosses.

Toutes les 5 minutes (GitHub Actions) :
  1. cherche les derniers fichiers radar « PRECIP » (RZC) dans le catalogue STAC de geo.admin.ch ;
  2. lit chaque fichier HDF5 (format ODIM), convertit en mm/h ;
  3. reprojette en Web Mercator et colorie (transparent là où il ne pleut pas) ;
  4. écrit site/radar-0.png (le plus récent) … radar-N.png et site/radar.json (heures + coins de l'image).

Source des données : MétéoSuisse (Open Government Data). Citer « Source : MétéoSuisse ».
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile

import h5py
import numpy as np
import requests
from PIL import Image
from pyproj import Transformer

STAC = "https://data.geo.admin.ch/api/stac/v1"
COLLECTION = "ch.meteoschweiz.ogd-radar-precip"
NB_IMAGES = 13           # la dernière heure, une image toutes les 5 min
ECART_MIN = 4            # minutes minimum entre deux images gardées
LARGEUR = 1000           # largeur de l'image produite (pixels)

# Échelle de couleurs (mm/h) proche de celle de MétéoSuisse.
PALIERS = [
    (0.1, (160, 210, 255, 150)),
    (0.5, (95, 170, 250, 175)),
    (1.0, (40, 120, 235, 195)),
    (2.0, (20, 80, 200, 205)),
    (4.0, (40, 175, 60, 210)),
    (6.0, (250, 230, 40, 215)),
    (10.0, (250, 150, 20, 220)),
    (20.0, (230, 40, 30, 225)),
    (40.0, (200, 30, 160, 230)),
    (60.0, (130, 40, 190, 235)),
]

session = requests.Session()
session.headers["User-Agent"] = "radar-alpes (bibliotheque des sales gosses)"


def lire_json(url, **kw):
    r = session.get(url, timeout=60, **kw)
    r.raise_for_status()
    return r.json()


def items_recents():
    """Les fichiers sont rangés par jour (item « AAAAMMJJ-ch ») : on lit aujourd'hui et hier."""
    items = []
    maintenant = dt.datetime.now(dt.timezone.utc)
    for decalage in (0, 1):
        jour = (maintenant - dt.timedelta(days=decalage)).strftime("%Y%m%d")
        try:
            items.append(lire_json(f"{STAC}/collections/{COLLECTION}/items/{jour}-ch"))
        except requests.HTTPError as e:
            print("Item", jour, "absent :", e)
    return items


def date_depuis_nom(nom):
    """RZC + aa + jjj + hhmm (jour de l'année) ou aaaammjjhhmm dans le nom du fichier."""
    base = os.path.basename(nom)
    m = re.search(r"(?i)rzc(\d{2})(\d{3})(\d{2})(\d{2})", base)
    if m:
        an, jour, h, mi = (int(x) for x in m.groups())
        return dt.datetime(2000 + an, 1, 1, h, mi, tzinfo=dt.timezone.utc) + dt.timedelta(days=jour - 1)
    m = re.search(r"(20\d{2})(\d{2})(\d{2})[T_-]?(\d{2})(\d{2})", base)
    if m:
        a, mo, j, h, mi = (int(x) for x in m.groups())
        return dt.datetime(a, mo, j, h, mi, tzinfo=dt.timezone.utc)
    return None


def fichiers_precip(items):
    """Liste (date, url) des fichiers HDF5 du produit PRECIP (RZC), du plus récent au plus ancien."""
    trouves = {}
    for it in items:
        for cle, a in (it.get("assets") or {}).items():
            href = a.get("href", "")
            nom = (cle + " " + href).lower()
            if not (href.lower().endswith(".h5") or "hdf" in str(a.get("type", "")).lower()):
                continue
            if "rzc" not in nom and "precip" not in nom:
                continue
            if "cpc" in os.path.basename(href).lower():
                continue
            quand = date_depuis_nom(href)
            if quand is None:
                v = a.get("datetime") or it.get("properties", {}).get("datetime") or a.get("updated")
                if v:
                    quand = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
            if quand:
                trouves[href] = quand
    return sorted(((q, h) for h, q in trouves.items()), reverse=True)


def texte(v):
    return v.decode() if isinstance(v, bytes) else str(v)


def lire_odim(chemin):
    with h5py.File(chemin, "r") as f:
        where = f["where"].attrs
        cle_data = "dataset1/data1"
        brut = f[cle_data + "/data"][:].astype("float64")
        what = f[cle_data + "/what"].attrs
        gain = float(what.get("gain", 1.0))
        offset = float(what.get("offset", 0.0))
        nodata = float(what.get("nodata", -1e30))
        undetect = float(what.get("undetect", -1e30))
        valeurs = brut * gain + offset
        valeurs[brut == undetect] = 0.0
        valeurs[brut == nodata] = np.nan
        grille = {
            "projdef": texte(where["projdef"]).strip(),
            "xscale": float(where["xscale"]), "yscale": float(where["yscale"]),
            "UL": (float(where["UL_lon"]), float(where["UL_lat"])),
            "UR": (float(where["UR_lon"]), float(where["UR_lat"])),
            "LL": (float(where["LL_lon"]), float(where["LL_lat"])),
            "LR": (float(where["LR_lon"]), float(where["LR_lat"])),
        }
        return valeurs, grille


def reprojeter(valeurs, g):
    """Grille du radar → image Web Mercator. Renvoie (tableau mm/h, coins lon/lat TL TR BR BL)."""
    vers_proj = Transformer.from_crs("EPSG:4326", g["projdef"], always_xy=True)
    x0, y0 = vers_proj.transform(*g["UL"])          # coin haut-gauche (bord extérieur)
    lonlat = [g["UL"], g["UR"], g["LR"], g["LL"]]
    vers_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    xs, ys = vers_merc.transform([p[0] for p in lonlat], [p[1] for p in lonlat])
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    hauteur = int(round(LARGEUR * (ymax - ymin) / (xmax - xmin)))
    X = xmin + (np.arange(LARGEUR) + 0.5) * (xmax - xmin) / LARGEUR
    Y = ymax - (np.arange(hauteur) + 0.5) * (ymax - ymin) / hauteur
    XX, YY = np.meshgrid(X, Y)
    merc_vers_proj = Transformer.from_crs("EPSG:3857", g["projdef"], always_xy=True)
    px, py = merc_vers_proj.transform(XX, YY)
    i = np.floor((px - x0) / g["xscale"]).astype(int)
    j = np.floor((y0 - py) / g["yscale"]).astype(int)
    ny, nx = valeurs.shape
    dedans = (i >= 0) & (i < nx) & (j >= 0) & (j < ny)
    sortie = np.full(XX.shape, np.nan)
    sortie[dedans] = valeurs[j[dedans], i[dedans]]
    # Contrôle : le coin bas-droit calculé doit tomber sur celui donné par le fichier.
    xr, yr = vers_proj.transform(*g["LR"])
    print(f"   grille {nx}x{ny} · écart coin bas-droit : {abs(x0 + nx * g['xscale'] - xr):.0f} m / {abs(y0 - ny * g['yscale'] - yr):.0f} m")
    vers_lonlat = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    coins = [vers_lonlat.transform(x, y) for x, y in [(xmin, ymax), (xmax, ymax), (xmax, ymin), (xmin, ymin)]]
    return sortie, [[round(c[0], 5), round(c[1], 5)] for c in coins]


def colorier(mmh):
    rgba = np.zeros(mmh.shape + (4,), dtype=np.uint8)
    v = np.nan_to_num(mmh, nan=-1)
    for seuil, couleur in PALIERS:
        rgba[v >= seuil] = couleur
    return Image.fromarray(rgba, "RGBA")


def explorer():
    print("== Collection ==")
    c = lire_json(f"{STAC}/collections/{COLLECTION}")
    print(json.dumps({k: c.get(k) for k in ("id", "title", "extent", "summaries")}, indent=1)[:3000])
    items = items_recents()
    print(f"== {len(items)} items récents ==")
    for it in items[:3]:
        print(it.get("id"), it.get("properties", {}).get("datetime"))
        for cle, a in list((it.get("assets") or {}).items())[:12]:
            print("   ", cle, "|", a.get("type"), "|", a.get("href"))
    print("== Fichiers PRECIP retenus ==")
    for q, h in fichiers_precip(items)[:10]:
        print(q.isoformat(), h)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sortie", default="site")
    p.add_argument("--explorer", action="store_true")
    a = p.parse_args()
    if a.explorer:
        explorer()
    os.makedirs(a.sortie, exist_ok=True)
    fichiers = fichiers_precip(items_recents())
    if not fichiers:
        sys.exit("Aucun fichier radar PRECIP trouvé dans le catalogue STAC.")
    gardes, dernier = [], None
    for q, h in fichiers:
        if dernier is None or (dernier - q).total_seconds() >= ECART_MIN * 60:
            gardes.append((q, h))
            dernier = q
        if len(gardes) >= NB_IMAGES:
            break
    images = []
    coins = None
    for n, (q, h) in enumerate(gardes):
        try:
            with tempfile.NamedTemporaryFile(suffix=".h5") as tmp:
                r = session.get(h, timeout=120)
                r.raise_for_status()
                tmp.write(r.content)
                tmp.flush()
                valeurs, grille = lire_odim(tmp.name)
            mmh, coins_image = reprojeter(valeurs, grille)
            coins = coins or coins_image
            nom = f"radar-{len(images)}.png"
            colorier(mmh).save(os.path.join(a.sortie, nom), optimize=True)
            images.append({"fichier": nom, "heure": q.isoformat().replace("+00:00", "Z"),
                           "pluie_max": None if np.all(np.isnan(mmh)) else round(float(np.nanmax(mmh)), 1)})
            print("OK", q.isoformat(), h, "max", images[-1]["pluie_max"], "mm/h")
        except Exception as e:  # une image ratée n'empêche pas les autres
            print("ÉCHEC", h, e)
    if not images:
        sys.exit("Aucune image radar n'a pu être produite.")
    with open(os.path.join(a.sortie, "radar.json"), "w") as f:
        json.dump({"source": "MétéoSuisse", "produit": "PRECIP (mm/h)", "genere": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "coins": coins, "images": images}, f, ensure_ascii=False)
    with open("page.html") as modele, open(os.path.join(a.sortie, "index.html"), "w") as f:
        f.write(modele.read())


if __name__ == "__main__":
    main()
