# MAGOH Spell 1.0 – Guida funzionale

Sviluppato da **Francesco D’Antoni** e **Lorena Bravi** con il [MAPPA Lab](https://www.mappalab.eu/en/home-eng/) dell’Università di Pisa.

MAGOH Spell è un plugin QGIS libero per lavorare con il modello dati archeologico MAGOH: archivi locali in GeoPackage, schede e relazioni, importazione in sola lettura dalle API MAGOH, esportazione PDF e conversione verso il Template GNA 1.6.0. Le maschere Intervento, Ritrovamento ed Evidenza archeologica (anche senza geometria) hanno un pulsante che apre la pagina corrispondente della [MAGOH Wiki](https://magoh-wiki.cfs.unipi.it/index.html).

## Requisiti e installazione

Il plugin è compatibile con QGIS 3.34–4.x, con Qt5 e Qt6, ed è stato provato su QGIS 3.34 con Qt5.

1. Salva il progetto QGIS e fai una copia dell’archivio MAGOH in uso.
2. Rimuovi i filtri temporanei con **Mostra tutti**.
3. Apri **Plugin → Gestisci e installa plugin → Installa da ZIP** e scegli lo ZIP senza estrarlo.
4. Riavvia QGIS, così la nuova versione e gli editor di campo vengono caricati correttamente.

Se dopo l’installazione i cappelli non compaiono:

1. In **Plugin → Gestisci e installa plugin → Installati** controlla che MAGOH Spell sia attivo e in versione 1.0.
2. Se il menu **Plugin → MAGOH Spell** è presente, il plugin è attivo: scegli **Mostra barra MAGOH Spell** oppure, con un clic destro sulle barre degli strumenti, attiva «MAGOH Spell».
3. Eventuali errori all’avvio compaiono nella barra dei messaggi; i dettagli sono in **Visualizza → Pannelli → Registro messaggi**, scheda «MAGOH Spell».

## I due pulsanti

**Cappello azzurro – Progetto e dati.** Crea o apre un progetto MAGOH, aggiunge o collega un archivio al progetto corrente, mostra lo stato della sessione, cambia la lingua (IT/EN) e apre Info MAGOH.

**Cappello viola – Funzioni e tool.** Apre un pannello agganciabile con tre schede:

- **Gestione.** *Schede e attributi*: ricerca in tutti gli attributi, in un singolo campo o per ID esatto; albero delle schede e dei collegamenti; zoom e apertura della maschera. *Cronologia e categorie*: anno singolo o intervallo, categorie, inclusione dei record non datati. Ricerca e filtro cronologico si usano in alternativa; **Mostra tutti** ripristina i filtri preesistenti.
- **Dati e conversione.** Importazione dalle API MAGOH (Provincia obbligatoria, Comune e area facoltativi) e convertitore MAGOH → GNA.
- **Esportazioni.** Schede PDF (metodo stabile o Atlas) e backup del GeoPackage.

Gli stessi comandi principali sono anche nel menu **Plugin → MAGOH Spell**.

### Ricerca nei campi a elenco

Nei campi a elenco (thesauri, cronologie, vocabolari GNA) la ricerca trova sia il codice salvato sia l’etichetta mostrata nella scheda, senza distinguere maiuscole, accenti e punteggiatura: «altomedievale» o «VIII sec. d.C.» trovano i record con `crono_iniziale` = 41.

Per Schede di Intervento, DSC e MOSI i risultati sono mostrati in due colonne, **ID scheda** e **Ubicazione**. L’ID è quello MAGOH di provenienza (`idscheda` per Interventi e DSC, ID del Ritrovamento per i MOSI); se manca, compare il codice GNA o il `fid` tecnico, preceduto dal nome del campo.

## Progetti e archivi

| Cartella | Contenuto |
|---|---|
| `data/` | GeoPackage operativo, cache API, MOPR di lavoro |
| `exports/pdf/` | schede PDF |
| `exports/gna/` | pacchetti generati con il Template GNA |
| `backups/`, `attachments/` | copie di sicurezza e allegati |

Ogni progetto QGIS usa un solo archivio MAGOH operativo. Il backup copia il GeoPackage salvato, ma non gli allegati né il file `.qgz`: per archiviare un progetto in modo completo conserva insieme `.qgz`, GeoPackage, allegati e risorse esterne.

Quando salvi la cancellazione di un Intervento vengono eliminati anche i Ritrovamenti e le Evidenze collegati, compresi quelli nascosti dai filtri; cancellando un Ritrovamento si eliminano le sue Evidenze.

## Codici delle schede

Quando salvi una nuova scheda, il progetto le assegna un codice leggibile, con sequenze distinte per Interventi, Ritrovamenti ed Evidenze. In un progetto con prefisso LSF (Lucca San Frediano) i primi codici sono `LSF-1`, `LSFR-1` e `LSFE-1`; le Evidenze con e senza geometria condividono la stessa sequenza.

- Nelle maschere il campo ID mostra questo codice, oppure l’ID MAGOH per i record importati. L’identificativo tecnico che collega le schede resta nel campo originale e non viene mostrato.
- Le schede già presenti mantengono i loro identificativi.
- Sulla mappa ricevono etichette automatiche solo le Schede di Intervento.
- Il prefisso si sceglie alla creazione del progetto e si può cambiare da **Plugin → MAGOH Spell**; rinominare il progetto non lo modifica.
- Il registro dei codici è salvato nel GeoPackage: le operazioni annullate non consumano codici e i codici delle schede cancellate non vengono riassegnati.
- **Confronta codici con una copia offline** elenca i codici in conflitto tra due archivi, da risolvere prima di unirli.

Il campo Precisione usa tre categorie: Preciso, Non Preciso e Non localizzabile.

## Conversione MAGOH → GNA

La conversione si svolge in un’unica finestra in tre passaggi: **1 · MOPR**, **2 · Conversione**, **3 · Risultato**.

1. **MOPR.** Scegli una geometria esistente oppure disegnala: clic sinistro per i vertici, clic destro per chiudere, Backspace per annullare l’ultimo vertice, Esc per interrompere. Con una geometria esistente viene usato l’intero layer; attivando «Usa solo le geometrie selezionate» viene usata solo la selezione di QGIS, e se non c’è nessuna selezione compare un avviso. Compila la scheda: al primo uso il codice professionista/ditta è vuoto e va inserito quello assegnato. Nel selettore FUR scegli il funzionario per nominativo ed ente; nel GeoPackage viene salvata la chiave FUR.
2. **Conversione.** Scegli MOPR, Interventi, Ritrovamenti ed eventualmente Evidenze. Il convertitore rispetta i filtri attivi in QGIS; salva o annulla prima le modifiche in sospeso. Esegui **Verifica conversione senza scrivere file**, poi **Genera il pacchetto completo GNA**.
3. **Risultato.** Leggi il report nella finestra del plugin, poi apri il `.qgz` del pacchetto e controlla DSC, MOSI e MOPR.

Vengono trasferite le geometrie interamente comprese nel perimetro MOPR, bordo incluso, con una tolleranza di circa 1 cm; quelle parzialmente esterne sono escluse per intero e documentate, senza ritagli.

La conversione crea la cartella `<nome progetto>_GNA` e uno ZIP con lo stesso nome; al loro interno il progetto QGIS e il GeoPackage mantengono il nome del progetto MAGOH. Il pacchetto contiene anche:

- `MAGOH_GNA_manifest.json`, con i conteggi e, nella sezione `filtro_MOPR`, le esclusioni spaziali;
- `conversion_log.csv`, che collega ogni scheda GNA alla scheda MAGOH di origine e riporta `codice_locale` per Interventi e Ritrovamenti e `codici_evidenze` per le Evidenze collegate ai MOSI.

Nel pacchetto GNA le schede create con MAGOH Spell non rimandano alla piattaforma MAGOH: OSS e DOZ indicano che il dato è stato creato con il plugin e ne riportano il codice.

Il pacchetto è un template di lavoro: può contenere campi da completare e non costituisce un conferimento. L’invio dei dati si fa con il plugin GNA ufficiale, che si installa separatamente dal Geoportale; MAGOH Spell non invia dati al Geoportale. Per la compilazione del MOPR vedi la [guida ministeriale](https://gna.cultura.gov.it/wiki/index.php/Compilare_il_MOPR).

Nel progetto MAGOH di lavoro il pacchetto viene caricato con gli stessi identificativi dei layer e lo stesso gruppo «SITI ARCHEOLOGICI E GEO-PALEONTOLOGICI (MOSI)» del Template ufficiale, con tutti e tre i layer MOSI anche se vuoti. In questo modo l’importazione dei dati già presenti nel GNA con il plugin ufficiale funziona anche qui, e i record scaricati finiscono nel GeoPackage del pacchetto. I layer GNA aggiunti dal plugin si possono togliere dalla legenda; MOSI, DSC e MOPR restano modificabili, mentre le tabelle dei vocabolari sono in sola lettura. Il progetto `.qgz` del pacchetto mantiene le regole e gli stili del Template GNA 1.6.0.

### Contenuto dei MOSI

- `OGN`: III livello del Ritrovamento e Ubicazione dell’Intervento, separati da « – » (per esempio «Reperti mobili – Certosa di Calci»), senza altri campi.
- `DES`: sintesi del Ritrovamento (ID e livelli I, II e III presenti), una riga vuota e la descrizione integrale della scheda Intervento. Il IV livello non viene elencato e il campo note dell’Intervento non sostituisce la descrizione.
- `GEN`: nei MOSI puntuali e multipunto contiene una nota fissa che dichiara la geometria puntuale una rappresentazione convenzionale; nei MOSI poligonali e lineari resta vuoto.
- `DTT` resta sempre vuoto. `DTSI` e `DTSF` non riportano gli estremi convenzionali di «Non determinato» usati da MAGOH (-3000000 e 5000).
- I punti coincidenti non vengono spostati né accorpati.

### Vocabolari

Il convertitore usa le etichette ufficiali del Template GNA 1.6.0 (`conversion/gna_vocab_snapshot_1_6.json`). Nel GeoPackage del pacchetto i campi collegati a un vocabolario (per esempio `OGM`, `OGD`, `FNTP`) ricevono la chiave del vocabolario, così QGIS mostra l’etichetta e non un valore tra parentesi. Le corrispondenze con i valori MAGOH sono in `conversion/mapping_magoh_gna_1_6.json` e `conversion/mapping.json`. Se un valore MAGOH non ha un lemma corrispondente, il campo resta vuoto e il report lo segnala, così la scelta resta all’operatore.

## Struttura del codice

| Modulo | Ruolo |
|---|---|
| `__init__.py` | punto di ingresso `classFactory` |
| `magoh_spell.py` | classe del plugin, finestre Progetto, API, PDF, GeoPackage, navigatore delle relazioni, pannello |
| `i18n.py` | interfaccia bilingue IT/EN |
| `ui_theme.py` | tema grafico condiviso |
| `api_*.py` | client REST in sola lettura, lettura delle geometrie GeoJSON (`api_geometry.py`), filtri spaziali, scrittura nel template |
| `template_integrity.py`, `related_filters.py`, `search_index.py` | integrità e cascate, propagazione padre/figlio, ricerca |
| `geographic_lookups.py`, `street_lookups.py`, `municipality_locator.py` | Comuni, indirizzi, toponimi |
| `security.py` | HTTPS, credenziali nel gestore QGIS, estrazione ZIP protetta |
| `gui/` | editor di campo, pannello cronologia, area API, workflow MOPR/GNA |
| `conversion/` | motore MAGOH → GNA, validatori, scrittura del Template |
| `data/`, `resources/` | template GeoPackage, confini comunali, indici, Template GNA 1.6.0 |

## Crediti e licenze

Il codice originale è distribuito con licenza GPL-2.0-or-later. Le illustrazioni dei cappelli sono di Vittoria Bellon (@icestorming), come indicato nella finestra dei crediti; la loro provenienza è descritta in `icons/PROVENIENZA.txt`. Le illustrazioni non rientrano nella licenza del codice e i diritti restano ai titolari. Il template incorporato deriva dal Template GNA 1.6.0 distribuito dal Ministero della Cultura attraverso il [Geoportale Nazionale per l’Archeologia](https://gna.cultura.gov.it/download.html).

[Licenze e attribuzioni](ATTRIBUZIONI.md) · [Portale MAGOH](https://magoh.cfs.unipi.it/web/guest) · [MAGOH Wiki](https://magoh-wiki.cfs.unipi.it/index.html)
