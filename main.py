import argparse
import sys
import warnings

from hecras_izvoz import NapakaIzvoza, izvozi


def main():
    parser = argparse.ArgumentParser(
        description="Izvoz aktivnega plana HEC-RAS 2D modela (območje, robni pogoji, nastavitve)."
    )
    parser.add_argument("projekt", nargs="?", help="mapa HEC-RAS projekta ali pot do .prj datoteke")
    parser.add_argument("id_obmocja", nargs="?", metavar="OBM_ID", help="unikatni identifikator območja (OBM_ID), npr. 1100_01")
    parser.add_argument("--izhod", help="izhodna mapa (privzeto: ./izhod)")
    args = parser.parse_args()

    # zagon brez argumentov (npr. gumb Run v PyCharmu): vprašaj za vrednosti
    if not args.projekt:
        args.projekt = input("Pot do HEC-RAS projekta (mapa ali .prj): ").strip("﻿ \"'")
    if not args.id_obmocja:
        args.id_obmocja = input("Identifikator območja (OBM_ID, npr. 1100_01): ").strip()
    if not args.izhod:
        args.izhod = input("Izhodna mapa [izhod]: ").strip().strip('"') or "izhod"

    # opozorila iz hecras_izvoz ujamemo in jih izpišemo enotno kot OPOZORILO
    napaka = None
    with warnings.catch_warnings(record=True) as ujeta:
        warnings.simplefilter("always")
        try:
            r = izvozi(args.projekt, args.id_obmocja, args.izhod)
        except NapakaIzvoza as e:
            napaka = str(e)
        except Exception as e:
            napaka = (f"nepričakovana napaka ({type(e).__name__}): {e}\n"
                      f"        Preverite, ali so datoteke projekta cele in je plan uspešno zagnan v HEC-RAS.")
    opozorila = [str(w.message) for w in ujeta if w.filename.endswith("hecras_izvoz.py")]

    if napaka:
        for o in opozorila:
            print(f"OPOZORILO: {o}", flush=True)  # da se izpiše pred NAPAKA (stderr)
        print(f"NAPAKA: {napaka}", file=sys.stderr)
        sys.exit(1)

    opozorila += [f"{ime} je bil spremenjen po zadnjem zagonu ({cas:%d.%m.%Y %H:%M:%S}) "
                  f"– izvoz morda ne odraža zadnjih sprememb; plan ponovno zaženite." for ime, cas in r["novejse"]]

    print(f"Projekt:        {r['projekt']}")
    print(f"Aktivni plan:   {r['plan']} – {r['plan_ime']}  ({r['plan_hdf'].name})")
    print(f"Zadnji zagon:   {r['zadnji_zagon']:%d.%m.%Y %H:%M:%S}")
    for o in opozorila:
        print(f"  OPOZORILO: {o}")
    print(f"2D območja:     {r['st_obmocij']}")
    print(f"RP linije:      {r['st_rp_linij']}")
    print(f"RP vrednosti:   {r['st_rp_vrednosti']} vrstic v {len(r['tabele_rp'])} tabelah: {', '.join(r['tabele_rp'])}")
    print(f"GPKG:           {r['gpkg']}")
    print(f"TXT:            {r['txt']}")


if __name__ == "__main__":
    main()
