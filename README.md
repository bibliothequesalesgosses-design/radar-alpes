# Radar Alpes

Convertit toutes les 5 minutes le radar de précipitations de **MétéoSuisse** (données ouvertes, produit PRECIP)
en images transparentes pour la carte 3D de l'appli **Bibliothèque des Sales Gosses**.

- `radar.py` : téléchargement (catalogue STAC geo.admin.ch), lecture HDF5, reprojection, couleurs.
- `.github/workflows/radar.yml` : lancé toutes les 5 min, publie sur GitHub Pages
  (`radar.json` + `radar-0.png` … `radar-12.png`, la dernière heure).
- `.github/workflows/garder-actif.yml` : un commit par mois pour que GitHub ne mette pas le radar en pause.

Source des données : MétéoSuisse.
