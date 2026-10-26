"""Izvoz aktivnega plana HEC-RAS 2D modela (v5.07+ / 6.x) v GPKG + TXT.

Izvozi tri sklope, vse z istim identifikatorjem območja (npr. 1100_01):
  1. območje hidravličnega modela (poligoni 2D Flow Area)      -> GPKG sloj
  2. robni pogoji: linije (GPKG sloj) + vrednosti (GPKG tabela) -> povezava prek bc_id
  3. nastavitve simulacije in začetni pogoji                    -> TXT
"""

import re
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import geopandas as gpd
import h5py
import numpy as np
import pandas as pd
import pyogrio
from pyproj import CRS
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon

ID_VZOREC = re.compile(r"^\d+_\d+$")
NI_NASTAVLJENO = 3.0e38  # HEC-RAS za prazno vrednost zapiše float32 max

# Opis vrednosti po tipu robnega pogoja: (ima_cas, opis stolpca vrednost, opis stolpca vrednost_2)
TIPI_RP = {
    "Flow Hydrographs": (True, "pretok", None),
    "Stage Hydrographs": (True, "gladina", None),
    "Stage and Flow Hydrographs": (True, "gladina", "pretok"),
    "Precipitation Hydrographs": (True, "padavine", None),
    "Lateral Inflow Hydrographs": (True, "pretok", None),
    "Uniform Lateral Inflow Hydrographs": (True, "pretok", None),
    "Groundwater Interflow": (True, "gladina_pv", None),
    "Normal Depths": (False, "naklon_trenja", None),
    "Rating Curves": (False, "gladina", "pretok"),
}

# Kratka šifra tipa robnega pogoja za ime tabele v GPKG
KODE_RP = {
    "Flow Hydrographs": "Q",
    "Stage Hydrographs": "H",
    "Stage and Flow Hydrographs": "HQ",
    "Precipitation Hydrographs": "P",
    "Lateral Inflow Hydrographs": "QL",
    "Uniform Lateral Inflow Hydrographs": "QLU",
    "Groundwater Interflow": "GW",
    "Normal Depths": "ND",
    "Rating Curves": "QH",
}

ENOTE = {
    "SI Units": {"pretok": "m3/s", "gladina": "m", "gladina_pv": "m", "padavine": "mm", "naklon_trenja": "m/m"},
    "US Customary": {"pretok": "cfs", "gladina": "ft", "gladina_pv": "ft", "padavine": "in", "naklon_trenja": "ft/ft"},
}


class NapakaIzvoza(Exception):
    pass


# ---------------------------------------------------------------- pomožne funkcije

def _txt(v):
    """Pretvori atribut iz HDF v berljiv niz."""
    if isinstance(v, (bytes, np.bytes_)):
        return v.decode("utf-8", errors="replace").strip()
    if isinstance(v, np.ndarray):
        return ", ".join(_txt(x) for x in v.tolist())
    if isinstance(v, (float, np.floating)):
        return "ni nastavljeno" if abs(v) > NI_NASTAVLJENO or np.isnan(v) else f"{v:g}"
    if isinstance(v, list):
        return ", ".join(_txt(x) for x in v)
    return str(v).strip()


def _atributi(f, pot):
    return {k: _txt(v) for k, v in f[pot].attrs.items()} if pot in f else {}


def _ras_datum(s):
    """'01jan2000 00:00:00', '31dec1999 2400', '01JAN2000,0000' -> datetime."""
    s = s.strip().replace(",", " ").replace(":", "")
    m = re.match(r"(\d{2}[A-Za-z]{3}\d{4})\s*(\d{4})(\d{2})?", s)
    if not m:
        raise ValueError(f"Neznan zapis datuma: {s!r}")
    dan = datetime.strptime(m.group(1).title(), "%d%b%Y")
    ura, minuta = int(m.group(2)[:2]), int(m.group(2)[2:])
    return dan + timedelta(hours=ura, minutes=minuta, seconds=int(m.group(3) or 0))


def _oblike(f, skupina):
    """Prebere HEC-RAS zapis oblik (Info/Parts/Points) -> seznam [seznam delov (n,2)] po objektih."""
    info = f[f"{skupina}/{'Polygon' if 'Polygon Info' in f[skupina] else 'Polyline'} Info"][()]
    predpona = "Polygon" if "Polygon Info" in f[skupina] else "Polyline"
    tocke = f[f"{skupina}/{predpona} Points"][()]
    deli = f[f"{skupina}/{predpona} Parts"][()] if f"{predpona} Parts" in f[skupina] else None
    rezultat = []
    for zac_t, st_t, zac_d, st_d in info:
        tocke_obj = tocke[zac_t:zac_t + st_t]
        if deli is None or st_d <= 1:
            rezultat.append([tocke_obj])
        else:
            rezultat.append([tocke_obj[z:z + n] for z, n in deli[zac_d:zac_d + st_d]])
    return rezultat


def _tekst_kljuci(pot):
    """Prebere tekstovno datoteko HEC-RAS (.prj/.pXX/.uXX) v seznam (ključ, vrednost)."""
    vrstice = Path(pot).read_text(encoding="latin-1").splitlines()
    return [tuple(v.split("=", 1)) for v in vrstice if "=" in v]


# ---------------------------------------------------------------- iskanje projekta in plana

def preveri_id(id_obmocja):
    if not ID_VZOREC.match(id_obmocja):
        raise NapakaIzvoza(f"Identifikator '{id_obmocja}' ni oblike npr. 1100_01 (številke_številke).")
    return id_obmocja


def najdi_prj(pot):
    """Sprejme mapo ali .prj datoteko in vrne pot do HEC-RAS .prj (ne ESRI projekcije)."""
    pot = Path(pot)
    kandidati = [pot] if pot.is_file() else sorted(pot.glob("*.prj"))
    for k in kandidati:
        if k.suffix.lower() == ".prj" and k.read_text(encoding="latin-1", errors="replace").startswith("Proj Title="):
            return k
    raise NapakaIzvoza(f"V '{pot}' ni HEC-RAS projekta (.prj z 'Proj Title=').")


def _je_zagnan(plan_hdf):
    try:
        with h5py.File(plan_hdf, "r") as f:
            return "Results" in f
    except OSError:
        return False


def najdi_aktivni_plan(prj):
    """Vrne (koda_plana, pot_do_plan_hdf).

    Najprej vzame 'Current Plan' iz .prj (zadnji shranjeni). Če ta plan nima rezultatov,
    vzame plan, katerega .pXX.hdf z rezultati je bil nazadnje spremenjen (zadnji zagnani).
    """
    kljuci = dict(_tekst_kljuci(prj))
    trenutni = kljuci.get("Current Plan", "").strip()
    if trenutni:
        hdf = prj.with_suffix(f".{trenutni}.hdf")
        if _je_zagnan(hdf):
            return trenutni, hdf
        warnings.warn(f"Trenutni plan {trenutni} nima rezultatov; iščem zadnji zagnani plan.")

    zagnani = [p for p in prj.parent.glob(f"{prj.stem}.p[0-9][0-9].hdf") if _je_zagnan(p)]
    if not zagnani:
        raise NapakaIzvoza("Projekt nima nobenega zagnanega plana (.pXX.hdf z rezultati).")
    zadnji = max(zagnani, key=lambda p: p.stat().st_mtime)
    return zadnji.suffixes[-2].lstrip("."), zadnji


# ---------------------------------------------------------------- branje sklopov

def preberi_crs(f):
    wkt = _txt(f.attrs.get("Projection", b""))
    if not wkt:
        warnings.warn("Plan HDF nima projekcije; izvoz bo brez koordinatnega sistema.")
        return None
    crs = CRS.from_wkt(wkt)
    epsg = crs.to_epsg(min_confidence=70)
    return CRS.from_epsg(epsg) if epsg else crs


def preberi_obmocje(f, id_obmocja, plan, crs):
    skupina = "Geometry/2D Flow Areas"
    if skupina not in f or "Attributes" not in f[skupina]:
        raise NapakaIzvoza("Plan nima 2D Flow Area.")
    imena = [_txt(a["Name"]) for a in f[f"{skupina}/Attributes"][()]]
    if "Polygon Info" in f[skupina]:
        oblike = _oblike(f, skupina)
    else:  # rezerva: obod posamezne 2D površine
        oblike = [[f[f"{skupina}/{ime}/Perimeter"][()]] for ime in imena]

    geometrije = []
    for deli in oblike:
        poligoni = [Polygon(d) for d in deli]
        geometrije.append(poligoni[0] if len(poligoni) == 1 else MultiPolygon(poligoni))

    info = _atributi(f, "Plan Data/Plan Information")
    return gpd.GeoDataFrame(
        {
            "id_obmocja": id_obmocja,
            "ime_2d": imena,
            "plan": plan,
            "plan_ime": info.get("Plan Name", ""),
            "geometrija": info.get("Geometry Filename", ""),
            "povrsina": [g.area for g in geometrije],
        },
        geometry=geometrije,
        crs=crs,
    )


def preberi_rp_linije(f, id_obmocja, crs):
    skupina = "Geometry/Boundary Condition Lines"
    if skupina not in f or "Attributes" not in f[skupina]:
        warnings.warn("Plan nima linij robnih pogojev.")
        return gpd.GeoDataFrame(
            columns=["id_obmocja", "bc_id", "ime", "obmocje_2d", "tip", "geometry"], geometry="geometry", crs=crs
        )
    atr = f[f"{skupina}/Attributes"][()]
    oblike = _oblike(f, skupina)
    geometrije = [LineString(d[0]) if len(d) == 1 else MultiLineString(d) for d in oblike]
    return gpd.GeoDataFrame(
        {
            "id_obmocja": id_obmocja,
            "bc_id": [f"{id_obmocja}_RP{i:02d}" for i in range(1, len(atr) + 1)],
            "ime": [_txt(a["Name"]) for a in atr],
            "obmocje_2d": [_txt(a["SA-2D"]) for a in atr],
            "tip": [_txt(a["Type"]) for a in atr] if "Type" in atr.dtype.names else "",
        },
        geometry=geometrije,
        crs=crs,
    )


def _ime_rp(ds, ime_dataseta):
    """Iz atributov (ali imena 'SA: M09 BCLine: X' / '2D: M09 BCLine: X') vrne (območje, BC linija)."""
    obmocje = _txt(ds.attrs.get("2D Flow Area", b"")) or _txt(ds.attrs.get("SA-2D", b""))
    linija = _txt(ds.attrs.get("BC Line", b""))
    if not obmocje:
        m = re.match(r"(?:SA|2D)\s*:\s*(.*?)(?:\s+BCLine:\s*(.*))?$", ime_dataseta)
        if m:
            obmocje, linija = m.group(1).strip(), (m.group(2) or linija).strip()
    return obmocje, linija


def preberi_rp_vrednosti(f, id_obmocja, linije, zacetek):
    """Vrne dolgo tabelo vrednosti robnih pogojev, povezano z linijami prek bc_id."""
    pot = "Event Conditions/Unsteady/Boundary Conditions"
    stolpci = ["id_obmocja", "bc_id", "ime", "obmocje_2d", "tip", "zap_st", "cas", "cas_h",
               "vrednost", "opis_vrednosti", "vrednost_2", "opis_vrednosti_2", "enota", "enota_2"]
    if pot not in f:
        warnings.warn("Plan nima vrednosti robnih pogojev (Event Conditions).")
        return pd.DataFrame(columns=stolpci)

    enote = ENOTE.get(_txt(f.attrs.get("Units System", b"SI Units")), ENOTE["SI Units"])
    bc_po_imenu = {(r.obmocje_2d, r.ime): r.bc_id for r in linije.itertuples()}
    vrstice = []
    for tip, skupina in f[pot].items():
        ima_cas, opis1, opis2 = TIPI_RP.get(tip, (True, "vrednost", None))
        for ime_ds, ds in skupina.items():
            if not isinstance(ds, h5py.Dataset):
                continue
            obmocje, linija = _ime_rp(ds, ime_ds)
            bc_id = bc_po_imenu.get((obmocje, linija)) or f"{id_obmocja}_{'RP' if linija else 'OBM'}_{linija or obmocje}"
            podatki = np.atleast_2d(ds[()].astype(float))
            if podatki.shape[0] == 1 and podatki.shape[1] != 2:  # npr. Normal Depth: ena vrednost
                podatki = podatki.T
            for i, vr in enumerate(podatki, start=1):
                vrstica = dict(id_obmocja=id_obmocja, bc_id=bc_id, ime=linija, obmocje_2d=obmocje, tip=tip, zap_st=i,
                               opis_vrednosti=opis1, enota=enote.get(opis1, ""),
                               opis_vrednosti_2=opis2, enota_2=enote.get(opis2, "") if opis2 else None)
                if ima_cas and len(vr) >= 2:
                    dni = float(vr[0])
                    vrstica.update(cas=(zacetek + timedelta(days=dni)).strftime("%Y-%m-%d %H:%M:%S"),
                                   cas_h=round(dni * 24, 6), vrednost=float(vr[1]),
                                   vrednost_2=float(vr[2]) if len(vr) > 2 else None)
                else:
                    vrstica.update(vrednost=float(vr[0]), vrednost_2=float(vr[1]) if len(vr) > 1 else None)
                vrstice.append(vrstica)
    return pd.DataFrame(vrstice, columns=stolpci)


# ---------------------------------------------------------------- nastavitve -> TXT

PLAN_KLJUCI = [
    "Simulation Date", "Computation Interval", "Output Interval", "Instantaneous Interval", "Mapping Interval",
    "Computation Time Step Use Courant", "Computation Time Step Use Time Series",
    "Computation Time Step Max Courant", "Computation Time Step Min Courant",
    "Computation Time Step Count To Double", "Computation Time Step Max Doubling",
    "Computation Time Step Max Halving", "UNET D2 TotalICTime", "UNET D2 RampUpFraction",
    "Write IC File", "Write IC File at Sim End", "IC Time",
]
UNSTEADY_KLJUCI = ("Use Restart", "Restart Filename", "Initial")


def _sekcija(naslov, pari):
    vrstice = ["", f"[{naslov}]"]
    vrstice += [f"{k} = {v}" for k, v in pari] if pari else ["(ni podatkov)"]
    return vrstice


def sestavi_nastavitve(f, id_obmocja, prj, plan, plan_hdf, linije, vrednosti):
    info = _atributi(f, "Plan Data/Plan Information")
    plan_txt = prj.with_suffix(f".{plan}")
    flow_txt = prj.parent / info.get("Flow Filename", "")

    plan_kljuci = [(k.strip(), v.strip()) for k, v in _tekst_kljuci(plan_txt) if k.strip() in PLAN_KLJUCI] \
        if plan_txt.is_file() else []
    plan_kljuci = list(dict.fromkeys(plan_kljuci))  # nastavitve 2D se ponovijo za vsako območje
    zacetni = [(k.strip(), v.strip()) for k, v in _tekst_kljuci(flow_txt) if k.strip().startswith(UNSTEADY_KLJUCI)] \
        if flow_txt.is_file() else []
    zacetni += [(f"HDF: {k}", v) for k, v in _atributi(f, "Event Conditions/Unsteady/Initial Conditions").items()]
    zacetni += [(k, v) for k, v in _atributi(f, "Plan Data/Plan Parameters").items() if "Initial" in k or "Ramp" in k]

    rp = [(r.bc_id, f"{r.ime} ({r.obmocje_2d}, {r.tip})") for r in linije.itertuples()]
    for (bc_id, tip), sk in vrednosti.groupby(["bc_id", "tip"]):
        rp.append((f"{bc_id} [{tip}]", f"{len(sk)} vrednosti, {sk['opis_vrednosti'].iat[0]} "
                                       f"{sk['vrednost'].min():g} – {sk['vrednost'].max():g} {sk['enota'].iat[0]}"))

    vrstice = [
        "HEC-RAS – nastavitve simulacije in začetni pogoji",
        f"Izvoženo: {datetime.now():%Y-%m-%d %H:%M:%S}",
    ]
    vrstice += _sekcija("IDENTIFIKATOR", [("id_obmocja", id_obmocja)])
    vrstice += _sekcija("PROJEKT", [
        ("projekt", prj), ("aktivni_plan", plan), ("plan_hdf", plan_hdf),
        ("verzija_hecras", _txt(f.attrs.get("File Version", b""))),
        ("enote", _txt(f.attrs.get("Units System", b""))),
    ])
    vrstice += _sekcija("PLAN (HDF Plan Information)", list(info.items()))
    vrstice += _sekcija(f"ČASOVNI KORAK IN INTERVALI (.{plan})", plan_kljuci)
    vrstice += _sekcija("PARAMETRI RAČUNA (HDF Plan Parameters)", list(_atributi(f, "Plan Data/Plan Parameters").items()))
    vrstice += _sekcija(f"ZAČETNI POGOJI ({flow_txt.name} + HDF)", zacetni)
    vrstice += _sekcija("ZAGON (HDF Results Summary)", list(_atributi(f, "Results/Unsteady/Summary").items()))
    vrstice += _sekcija("ROBNI POGOJI (povezava z GPKG prek bc_id)", rp)
    vrstice += _sekcija("ŠIFRE TIPOV RP (konec imena tabele v GPKG)", [(v, k) for k, v in KODE_RP.items()])
    return "\n".join(vrstice) + "\n"


# ---------------------------------------------------------------- glavni izvoz

def izvozi(pot_projekta, id_obmocja, izhod="izhod"):
    """Izvozi aktivni plan v <izhod>/<id_obmocja>/. Vrne slovar s potmi in povzetkom."""
    preveri_id(id_obmocja)
    prj = najdi_prj(pot_projekta)
    plan, plan_hdf = najdi_aktivni_plan(prj)

    with h5py.File(plan_hdf, "r") as f:
        crs = preberi_crs(f)
        zacetek = _ras_datum(_atributi(f, "Plan Data/Plan Information")["Simulation Start Time"])
        obmocje = preberi_obmocje(f, id_obmocja, plan, crs)
        linije = preberi_rp_linije(f, id_obmocja, crs)
        vrednosti = preberi_rp_vrednosti(f, id_obmocja, linije, zacetek)
        nastavitve = sestavi_nastavitve(f, id_obmocja, prj, plan, plan_hdf, linije, vrednosti)

    mapa = Path(izhod) / id_obmocja
    mapa.mkdir(parents=True, exist_ok=True)
    gpkg = mapa / f"{id_obmocja}_hecras.gpkg"
    txt = mapa / f"{id_obmocja}_nastavitve.txt"
    try:
        gpkg.unlink(missing_ok=True)
    except PermissionError:
        raise NapakaIzvoza(f"'{gpkg}' je odprt v drugem programu (npr. QGIS). Zaprite ga in ponovite izvoz.")

    # vsak robni pogoj dobi svojo tabelo vrednosti, poimenovano <bc_id>_<šifra tipa>, npr. 1100_01_rp01_Q;
    # ime tabele je zapisano tudi pri liniji
    vrednosti["koda_tipa"] = vrednosti["tip"].map(KODE_RP).fillna("X")
    kode = vrednosti.groupby("bc_id")["koda_tipa"].first()
    tabele = {bc_id: re.sub(r"\W", "_", bc_id.replace("_RP", "_rp").replace("_OBM", "_rp_obm")) + f"_{kode[bc_id]}"
              for bc_id in sorted(kode.index)}
    linije["koda_tipa"] = linije["bc_id"].map(kode)
    linije["tabela_vrednosti"] = linije["bc_id"].map(tabele)

    obmocje.to_file(gpkg, layer=f"{id_obmocja}_ohm", driver="GPKG")
    linije.to_file(gpkg, layer=f"{id_obmocja}_rp_linije", driver="GPKG")
    for bc_id, tabela in tabele.items():
        sk = vrednosti[vrednosti["bc_id"] == bc_id].dropna(axis=1, how="all")
        pyogrio.write_dataframe(sk, gpkg, layer=tabela, driver="GPKG")
    txt.write_text(nastavitve, encoding="utf-8-sig")  # BOM, da šumnike prav prikaže tudi Beležnica

    return {"projekt": prj, "plan": plan, "plan_hdf": plan_hdf, "gpkg": gpkg, "txt": txt,
            "st_obmocij": len(obmocje), "st_rp_linij": len(linije), "st_rp_vrednosti": len(vrednosti),
            "tabele_rp": list(tabele.values())}
