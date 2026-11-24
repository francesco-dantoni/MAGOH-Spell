# MAGOH Spell 1.0

Sviluppato da **Francesco D’Antoni** e **Lorena Bravi**.

Versione **1.0**. Plugin QGIS libero per lavorare con il modello dati archeologico MAGOH: archivi locali in GeoPackage, schede e relazioni, importazione in sola lettura dalle API MAGOH, esportazione PDF e conversione verso il Template GNA 1.6.0.

Le maschere Intervento, Ritrovamento ed Evidenza archeologica (anche senza geometria) includono un pulsante che apre la relativa pagina della MAGOH Wiki. I vecchi riquadri Info delle maschere non vengono più mostrati.

## Requisiti e installazione

Compatibilità dichiarata: QGIS 3.34–4.x, Qt5 e Qt6. Collaudo eseguito su QGIS 3.34 (Qt5). L'intervallo dei metadati non equivale al collaudo di ogni patch o sistema operativo.

1. Salvare il progetto QGIS e fare un backup dell'archivio MAGOH in uso.
2. Togliere i filtri temporanei con **Mostra tutti**.
3. **Plugin → Gestisci e installa plugin → Installa da ZIP**, scegliere lo ZIP senza estrarlo.
4. Riavviare QGIS per caricare la nuova versione e ripristinare correttamente gli editor di campo.

Se dopo l'installazione i cappelli non compaiono:

1. **Plugin → Gestisci e installa plugin → Installati**: MAGOH Spell deve avere la spunta attiva e versione 1.0.
2. Se esiste il menu **Plugin → MAGOH Spell**, il plugin è attivo: scegliere **Mostra barra MAGOH Spell**, oppure clic destro sulle barre degli strumenti e spuntare «MAGOH Spell».
3. Se un passaggio dell'avvio non è riuscito, la barra messaggi lo segnala; il dettaglio è in **Visualizza → Pannelli → Registro messaggi**, scheda «MAGOH Spell».

## I due pulsanti

**Cappello azzurro, Progetto e dati.** Crea o apre un progetto MAGOH, aggiunge o collega un archivio al progetto corrente, mostra lo stato della sessione, cambia lingua IT/EN, apre Info MAGOH.

**Cappello viola, Funzioni e tool.** Pannello agganciabile con tre schede:

- **Gestione**: *Schede e attributi* (ricerca su tutti gli attributi, su un campo o per ID esatto; albero di schede e collegamenti; zoom e apertura della maschera) e *Cronologia e categorie* (anno singolo o intervallo, categorie, inclusione dei record non datati). Ricerca e cronologia sono alternative; **Mostra tutti** ripristina i filtri preesistenti. Nei campi a elenco (thesauri, cronologie, vocabolari GNA) la ricerca trova sia il codice salvato sia l'etichetta mostrata nella scheda, senza distinguere maiuscole, accenti e punteggiatura: «altomedievale» o «VIII sec. d.C.» trovano i record con `crono_iniziale` = 41. I risultati di Schede di Intervento, DSC e MOSI compaiono in due colonne, **ID scheda** e **Ubicazione**: l'ID è quello MAGOH di provenienza (`idscheda` per Interventi e DSC, ID del Ritrovamento per i MOSI); se manca, si vede il codice GNA o il `fid` tecnico preceduto dal nome del campo.
- **Dati e conversione**: importazione dalle API MAGOH (Provincia obbligatoria, Comune e area facoltativi) e convertitore MAGOH → GNA.
- **Esportazioni**: schede PDF (metodo stabile o Atlas) e backup del GeoPackage.

Gli stessi comandi principali sono nel menu **Plugin → MAGOH Spell**.

## Struttura di un progetto

| Cartella | Contenuto |
|---|---|
| `data/` | GeoPackage operativo, cache API, MOPR di lavoro |
| `exports/pdf/` | schede PDF |
| `exports/gna/` | pacchetti generati con il Template GNA |
| `backups/`, `attachments/` | copie di sicurezza e allegati |

Un solo archivio MAGOH operativo per progetto QGIS. Il backup copia il GeoPackage salvato, non allegati o file `.qgz`: per archiviare un progetto in modo riproducibile conservare insieme `.qgz`, GeoPackage, allegati e risorse esterne.

La cancellazione di un Intervento, al salvataggio, coinvolge i Ritrovamenti e le Evidenze collegati anche se nascosti dai filtri; la cancellazione di un Ritrovamento coinvolge le sue Evidenze.

## Conversione MAGOH → GNA

Una sola finestra in tre passaggi: **1 · MOPR**, **2 · Conversione**, **3 · Risultato**.

1. Creare il MOPR scegliendo una geometria esistente o disegnandola (clic sinistro vertici, clic destro termina, Backspace annulla l'ultimo vertice, Esc annulla). Con una geometria esistente si usa l'intero layer; spuntando «Usa solo le geometrie selezionate» si usa solo la selezione QGIS, e senza selezione compare un avviso. Compilare la maschera: il codice professionista/ditta è vuoto al primo uso e non va inventato.
2. Scegliere MOPR, Interventi, Ritrovamenti ed eventualmente Evidenze. I filtri QGIS attivi vengono rispettati; salvare o annullare le modifiche pendenti.
3. Eseguire **Verifica conversione senza scrivere file**, poi **Genera il pacchetto completo GNA**.
4. Aprire il `.qgz` autonomo del pacchetto e controllare DSC, MOSI e MOPR. Il report resta nella finestra del plugin; il filtro MOPR è registrato in `MAGOH_GNA_manifest.json` e il collegamento fra schede GNA e schede MAGOH di origine in `conversion_log.csv`, entrambi dentro il pacchetto.

Le geometrie interamente dentro il perimetro MOPR (bordo compreso, tolleranza circa 1 cm) vengono trasferite; le altre sono escluse e documentate, senza ritagli. Il pacchetto è un template di lavoro: può contenere campi da completare e non certifica un conferimento. L'invio si fa con il plugin GNA ufficiale; MAGOH Spell non invia dati al Geoportale. Guida ministeriale: <https://gna.cultura.gov.it/wiki/index.php/Compilare_il_MOPR>.

Nel progetto MAGOH di lavoro il pacchetto viene caricato con gli stessi identificativi dei layer e lo stesso gruppo «SITI ARCHEOLOGICI E GEO-PALEONTOLOGICI (MOSI)» del Template ufficiale, con tutti e tre i layer MOSI anche se vuoti: così l'importazione dei dati già presenti nel GNA con il plugin GNA ufficiale funziona anche qui, e i record scaricati finiscono nel GeoPackage del pacchetto. I layer GNA aggiunti dal plugin si possono togliere dalla legenda; MOSI, DSC e MOPR restano modificabili, le tabelle dei vocabolari restano in sola lettura. Il progetto `.qgz` autonomo del pacchetto mantiene le regole e gli stili del Template GNA 1.6.0.

### Contenuto dei MOSI

- `OGN`: III livello del Ritrovamento e Ubicazione dell'Intervento, separati da « – » (per esempio «Reperti mobili – Certosa di Calci»). Nessun altro campo.
- `DES`: sintesi del Ritrovamento (ID e livelli I, II e III presenti), una riga vuota e la descrizione integrale della scheda Intervento. Nessun elenco del IV livello; il campo note dell'Intervento non sostituisce la descrizione.
- `GEN`: nei MOSI puntuali e multipunto, nota fissa che dichiara la geometria puntuale come rappresentazione convenzionale; vuoto nei poligonali e lineari.
- `DTT`: sempre vuoto. `DTSI` e `DTSF` non riportano gli estremi di «Non determinato» di MAGOH (-3000000 e 5000).
- I punti coincidenti non vengono spostati né accorpati.

### Vocabolari

Il convertitore lavora con le etichette ufficiali del Template GNA 1.6.0 (`conversion/gna_vocab_snapshot_1_6.json`); nel GeoPackage del pacchetto i campi collegati a un vocabolario (per esempio `OGM`, `OGD`, `FNTP`) ricevono la chiave del vocabolario, così QGIS mostra l'etichetta e non un valore tra parentesi. Le corrispondenze dai valori MAGOH stanno in `conversion/mapping_magoh_gna_1_6.json` e `conversion/mapping.json`; quando un valore MAGOH non ha un lemma corrispondente, il campo resta vuoto e il report lo segnala, invece di sceglierne uno al posto dell'operatore.

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

## Crediti

Illustrazioni dei cappelli: Vittoria Bellon (@icestorming), come indicato nella finestra dei crediti; provenienza e trattamento delle immagini in `icons/PROVENIENZA.txt`. Le illustrazioni non sono coperte dalla licenza del codice: i diritti restano ai titolari. Sito MAGOH: <https://magoh.cfs.unipi.it/web/guest>.

Nel selettore FUR scegli il funzionario per nominativo e per ente. Il GeoPackage conserva la chiave FUR. Il plugin GNA per il conferimento si installa separatamente dal Geoportale. Il codice originale è rilasciato sotto GPL-2.0-or-later. [Licenze e attribuzioni](ATTRIBUZIONI.md).

## Cartella e fonte del progetto GNA

La conversione crea la cartella `<nome progetto>_GNA` e lo ZIP con lo stesso nome. Al loro interno il progetto QGIS e il GeoPackage mantengono il nome del progetto MAGOH. Il template incorporato deriva dal Template GNA 1.6.0 distribuito dal Ministero della Cultura attraverso il [Geoportale Nazionale per l’Archeologia](https://gna.cultura.gov.it/download.html).

## Codici delle schede locali

Al salvataggio di una nuova scheda il progetto assegna un codice leggibile, con una sequenza distinta per Interventi, Ritrovamenti ed Evidenze. Per Lucca San Frediano i primi codici sono LSF-1, LSFR-1 e LSFE-1. Le evidenze con e senza geometria condividono la serie E. Nelle maschere il campo ID della scheda mostra questo codice, oppure l'ID MAGOH per i record importati; l'identificativo tecnico che collega le schede resta nel campo originale ma non è visibile. Le schede già presenti mantengono i loro identificativi. Solo le Schede di Intervento ricevono etichette automatiche sulla mappa. Nel pacchetto GNA una scheda creata con MAGOH Spell non rimanda alla piattaforma MAGOH: OSS e DOZ indicano che il dato è stato creato con il plug-in e ne riportano il codice.

Il campo Prefisso codici scheda è modificabile durante la creazione e dal menu Plugin → MAGOH Spell. Il registro è conservato nel GeoPackage. Cambiare il nome del progetto non modifica il prefisso. Gli annullamenti non consumano codici e i codici delle schede cancellate restano riservati. Il comando Confronta codici con una copia offline elenca i conflitti tra due archivi. La risoluzione precede una futura unione dei dati.

Precisione usa le categorie Preciso, Non Preciso e Non localizzabile. Il CSV di conversione include codice_locale per Interventi e Ritrovamenti e codici_evidenze per le Evidenze collegate ai MOSI.
