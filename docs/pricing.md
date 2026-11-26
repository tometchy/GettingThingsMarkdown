# Roboczy cennik i ekonomia pełnego Git — 2026-11-26

Obowiązujące wymagania: [MVP-SPECIFICATION.md](MVP-SPECIFICATION.md). Branding oddzielnie: [plan-branding.md](plan-branding.md). Starsze analizy LFS/S3 i rezerwacji 5× nie są obowiązującą architekturą; ich modele należy zastąpić lub zaktualizować.

| ID | Logiczny limit historii | USD/mies. (roboczo) | USD/rok (roboczo) | Maks. dane A+B (przed narzutem) |
|---|---:|---:|---:|---:|
| basic | 10 GiB | 1.99 | 17.88 | 30 GiB |
| plus | 75 GiB | 5.99 | 59.88 | 225 GiB |
| pro | 500 GiB | 24.99 | 239.88 | 1500 GiB |
| max | 2048 GiB | ~99 (99 / 99.99) | TBD | 6144 GiB |

Miesięczny trial po weryfikacji e-maila, przedłużanie admina, developer-only 100 lat. Stripe i zgodne z politykami platform mobilne zakupy/zarządzanie z każdej aplikacji. Techniczne ID stabilne, katalog 4 planów i 8 okresów/cen docelowo; brak istniejących użytkowników, nie potrzebujemy migracji. Cena max niezatwierdzona; roczny rabat nie może niszczyć marży. Nie twórz płatnych Price IDs przed decyzją. Ceny podstawowe także nie są dowodem rentowności.

## Model kosztów i kryterium maksymalnego wykorzystania

Q to logiczna ilość danych historii (unikalne wersje użytkownika), W to rozmiar working tree, zwykle <= Q z zastrzeżeniem formatu i deduplikacji. Do szacowania konserwatywnie przyjmij W=Q, oba Git=Q, bez korzyści kompresji. Koszt A=(Q+W)*cA/uA, koszt B=Q*cB/uB, gdzie c to koszt jednostki użytecznej pojemności po RAID, u to bezpieczne wykorzystanie puli. Do tego osobno infrastruktura poza storage, transfer i replikacja, retencja/snapshoty, operacje tymczasowe, standby/rebuild, monitoring, podatek/VAT (odróżnij przychód netto), opłaty procesora lub sklepu, support. Nie licz dwukrotnie CPU/RAM jeśli zawarte w cenie storage node. Weryfikuj narzuty Git i ciphertext, maile, vault oraz konfigurację, które nie są rozliczane użytkownikowi. Oblicz scenariusze różnych poziomów zapełnienia planu i puli, ale obowiązkowo 100% max i pełne wykorzystanie pozostałych planów. Nie zakładaj oszczędności z nieużywania produktu przez max.

Przykład 2 TiB: A=4 TiB, B=2 TiB, razem 6 TiB. Przy 75% wykorzystaniu puli potrzeba 8 TiB kupionej użytecznej pojemności (5.33 A + 2.67 B), przed dodatkowym narzutem, retencją i zapasem odbudowy. Współdzielone dyski są skalowane w górę i w dół; brak per-tenant pełnej rezerwacji. Jeśli używamy RAID mirroring, koszt pojemności użytecznej może być ok. 2× wyższy od raw.

Przykładowe publiczne ceny (26 XI 2026, do potwierdzenia u dostawcy przed zakupem):
- Hetzner SX65-3 4×22 TB HDD, ~292.30 EUR/mies. bez IPv4, wg cennika aktualizacji z czerwca 2026: https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/ ; parametry https://docs.hetzner.com/robot/dedicated-server/server-lines/sx-server/ .
- OVH ADVANCE-STOR 2×24 TB SAS, 384.99 EUR/mies., publiczna aktualizacja sierpień 2026: https://blog.ovhcloud.com/en/posts/dedicated-servers-pricing-update-h2-2026/ .
- To odpowiednio ~80.0 TiB i ~43.7 TiB surowej pojemności. Ilustracyjnie A na Hetzner i B na OVH, przy 75% wykorzystaniu i bez lokalnego RAID, sam proporcjonalny koszt obu node'ów dla pełnego max wynosi około 43 EUR/mies. Przy mirroringu obu lokalnych pul ok. 86 EUR/mies., przed kosztami dodatkowymi. Nie jest to zalecenie wyłączenia RAID ani finalny model: koszt zależy od konfiguracji, dostępności, retencji, obciążenia i wielkości floty, nie wolno utożsamiać raw z usable. Nie mieszaj EUR z USD bez aktualnego kursu. Dodatkowe wymagania niezależnej retencji i failover mogą istotnie zwiększyć koszt. Zarządzane SSD block storage w przeliczeniu na TB jest zwykle znacznie droższe i nie należy zakładać, że pozwoli uzyskać cenę ~99 USD.

Marża kontrybucyjna = (przychód netto po VAT - wszystkie koszty zmienne i przypisany koszt infrastruktury)/przychód netto. Przykład wyłącznie scenariuszowy USD: dla 99 USD przychodu netto przed kosztami, przy pełnym miesięcznym koszcie 50/70/90 USD marża odpowiednio ~49.5%/29.3%/9.1%. Cena przy wymaganej marży m = pełny koszt/(1-m). Przy 40% marży i 99 USD netto wszystkie koszty muszą się zmieścić w 59.40 USD; 40% to ilustracyjny próg, nie zatwierdzona marża. Weryfikuj roczne rabaty, billing przez store i podatek. Wymóg: max dodatni przy 100% wykorzystaniu i wyraźnie wyższa marża procentowa niż basic i plus przy porównywalnych założeniach, bez subsydiowania. Jeżeli nie da się tego spełnić za ~99 USD, przedstaw alternatywę do decyzji: wyższa cena, mniejszy limit albo inna bezpieczna infrastruktura, bez osłabiania gwarancji.

## Warunki uruchomienia komercyjnego
Zaktualizuj i zweryfikuj model kosztów w skryptach, realne cenniki obu dostawców, wydajność storage i procesów przy dużej liczbie klientów, retencję, replikację, failover, opłaty, podatki, cenę roczną i rentowność wszystkich planów. Ustal dopuszczalne limity zasobów/transferu w sposób jawny, nie obiecuj nierealnych parametrów. Stripe test mode; nie publikuj stawek ani nie uruchamiaj płatnych zasobów bez zgody. Zachowaj wszystkie kryteria bezpieczeństwa i akceptacji MVP z głównej specyfikacji.

## Obliczenia wykonywalne
`python3 scripts/full_git_cost_model.py` modeluje cztery plany, 25/50/75/100% wykorzystania, miesięczne i roczne rozliczenie, jawne narzuty na retencję i odbudowę, VAT, płatności, support, transfer/compute i wspólne usługi. `--payment-fraction 0.30` pozwala zbadać scenariusz sklepu, `--max-year-usd` podać hipotetyczną roczną cenę. Bez tej ceny roczny gate zwraca false. Parametry są scenariuszowe, nie stanowią oferty ani zweryfikowanego kosztu; wymagany margines przewagi max domyślnie 5 pp jest ilustracyjny. Model proporcjonalny nie uwzględnia zaokrąglenia liczby węzłów, minimalnej floty, realnego rozkładu klientów, całkowitej odbudowy w ocalałej chmurze ani pomiarów IOPS. Nie jest dowodem rentowności. Następnie należy uzupełnić model floty oraz zweryfikować koszty retencji i failover.
