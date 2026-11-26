# 1.0 – 26 novembre 2026

Prima versione pubblica di MAGOH Spell.

- **Progetti MAGOH in QGIS.** Creazione e apertura di progetti con archivio GeoPackage locale; interfaccia in italiano e inglese.
- **Schede e collegamenti.** Maschere per Interventi, Ritrovamenti ed Evidenze, con un pulsante che apre la pagina corrispondente della MAGOH Wiki, e navigazione dei collegamenti tra schede. Negli elenchi ogni Ritrovamento mostra il III livello e ogni Evidenza il IV livello dopo l’identificativo.
- **Codici delle schede.** Al salvataggio ogni nuova scheda riceve un codice leggibile, con sequenze distinte per tipo (per esempio `LSF-1`, `LSFR-1`, `LSFE-1`); i record importati mantengono l’ID MAGOH. Il comando Confronta codici con una copia offline segnala i conflitti tra due archivi.
- **Ricerca e filtri.** Ricerca per attributo, ID o voce di vocabolario; filtri per cronologia e categoria.
- **Importazione dalle API MAGOH.** In sola lettura, per Provincia, Comune o area. Il III livello dei ritrovamenti viene ricavato all’interno del II livello della scheda, così le denominazioni presenti in più rami (per esempio «reperti mobili») ricevono il codice corretto.
- **Esportazioni.** Schede PDF, con metodo stabile o Atlas, e backup del GeoPackage.
- **Conversione nel Template GNA 1.6.0 del 26 settembre 2026.** MOPR, DSC e MOSI in un pacchetto da controllare e completare, con i vocabolari ufficiali in locale nella cartella `vocabolari_GNA`.
  - Il vocabolario dei funzionari (FUR) e quello degli enti sono quelli ufficiali completi: il plugin GNA riconosce il funzionario responsabile e ne compila l’indirizzo e-mail.
  - Nessun testo supera la lunghezza dichiarata dal Template: DES, OSS e gli altri campi oltre il limite vengono ridotti senza spezzare le parole, segnalati nel report e chiusi dai riferimenti URL, sempre conservati; una DES ridotta rimanda alla scheda Intervento MAGOH, dove il testo è integrale.
  - `OGM_new` riceve la modalità di individuazione; quando in MAGOH la modalità è una fonte («dati di archivio», «dati bibliografici»), registrata in FNTP, la modalità si ricava dal tipo di intervento.
  - `DTR_new` riceve il periodo GNA coerente con gli anni: se gli anni non rientrano nel periodo MAGOH si sceglie il periodo più breve che li comprende; se mancano, DTSI e DTSF prendono gli estremi del periodo.
  - Compilatore e responsabile delle schede (CMPN, CMC, CMR) vengono dalla scheda di progetto, nella forma «Cognome, Nome» richiesta dal Template.
  - Il report si legge nella finestra Risultato; `conversion_log.csv` collega ogni scheda GNA alla scheda di origine e il manifesto riporta le esclusioni spaziali nella sezione `filtro_MOPR`. Le schede create con MAGOH Spell riportano in OSS e DOZ il proprio codice.
