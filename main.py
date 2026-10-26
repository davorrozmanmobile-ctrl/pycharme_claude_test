import argparse
import sys

from hecras_izvoz import NapakaIzvoza, izvozi


def main():
    parser = argparse.ArgumentParser(
        description="Izvoz aktivnega plana HEC-RAS 2D modela (območje, robni pogoji, nastavitve)."
    )
    parser.add_argument("projekt", help="mapa HEC-RAS projekta ali pot do .prj datoteke")
    parser.add_argument("id_obmocja", help="unikatni identifikator območja, npr. 1100_01")
    parser.add_argument("--izhod", default="izhod", help="izhodna mapa (privzeto: ./izhod)")
    args = parser.parse_args()

    try:
        r = izvozi(args.projekt, args.id_obmocja, args.izhod)
    except NapakaIzvoza as e:
        print(f"NAPAKA: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Projekt:        {r['projekt']}")
    print(f"Aktivni plan:   {r['plan']}  ({r['plan_hdf'].name})")
    print(f"2D območja:     {r['st_obmocij']}")
    print(f"RP linije:      {r['st_rp_linij']}")
    print(f"RP vrednosti:   {r['st_rp_vrednosti']} vrstic")
    print(f"GPKG:           {r['gpkg']}")
    print(f"TXT:            {r['txt']}")


if __name__ == "__main__":
    main()
