# 1.0

Il report di conversione si consulta nella finestra Risultato. Il pacchetto GNA include il collegamento tra schede di origine e destinazione in conversion_log.csv e le esclusioni spaziali nella sezione filtro_MOPR del manifesto. I vocabolari offline mostrano i nominativi dei funzionari associati agli enti e conservano le chiavi FUR. I recapiti e-mail sono esclusi.

Aggiunti codici per tipo di scheda e verifica dei conflitti offline. Uniformata la Precisione. Il pannello Cronologia recupera dal progetto i layer attivi dopo la loro sostituzione e ricalcola categorie e intervallo.

Ogni scheda mostra un solo identificativo, nel campo ID del Template: il codice della scheda creata nel progetto (es. LUC-1) oppure l'ID MAGOH dei record importati. L'ID tecnico negativo non compare più; una scheda non ancora salvata indica che il codice sarà assegnato al salvataggio. Lo stesso identificativo compare nei collegamenti tra schede e nelle etichette delle Schede di Intervento; Ritrovamenti ed Evidenze non ricevono più etichette automatiche.

Il III livello dei ritrovamenti importati da API si ricava all'interno del II livello della scheda: denominazioni presenti in più II livelli (es. «reperti mobili») non ricevono più il codice di un altro ramo. All'apertura del progetto i codici già importati vengono riallineati, senza modificare le denominazioni.

Nel pacchetto GNA le schede create con MAGOH Spell non rimandano alla piattaforma MAGOH: OSS e DOZ riportano che il dato è stato creato con il plug-in, con il codice della scheda.
