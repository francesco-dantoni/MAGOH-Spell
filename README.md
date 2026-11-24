# MAGOH Spell

MAGOH Spell è un plugin QGIS per documentare e consultare interventi, ritrovamenti ed evidenze archeologiche secondo il modello MAGOH. Consente di lavorare in un GeoPackage locale, importare dati dalle API MAGOH, produrre schede PDF e convertire i dati nel Template GNA 1.6.0.

Sviluppato da **Francesco D’Antoni** e **Lorena Bravi**.

## Installazione in QGIS

1. Apri la sezione **Releases** del repository e scarica l’allegato della versione 1.0 denominato `MAGOH_Spell_V1.0.zip`.
2. In QGIS apri **Plugin → Gestisci e installa plugin → Installa da ZIP** e seleziona l’archivio ancora compresso.
3. Attiva **MAGOH Spell** e apri **Plugin → MAGOH Spell**. Da **Progetto e dati** puoi creare un progetto o aprirne uno esistente.

Prima di aggiornare un progetto già in uso, salva le modifiche e crea una copia del GeoPackage. I metadati dichiarano compatibilità da QGIS 3.34 a 4.x.

## Lavorare con le schede

- Consulta i collegamenti tra interventi, ritrovamenti ed evidenze. Per un progetto con prefisso LSF, le rispettive serie locali iniziano da `LSF-1`, `LSFR-1` e `LSFE-1`. Le schede importate mostrano il loro identificativo MAGOH.
- Cerca una scheda per attributo, ID o voce di vocabolario. Il pannello **Cronologia e categorie** consente di esplorare intervalli cronologici e livelli di caratterizzazione.
- Esporta schede PDF oppure genera il progetto GNA nella cartella con suffisso `_GNA`. Il registro di conversione permette di risalire alle schede di origine.

[Guida illustrata (PDF)](docs/MAGOH_Spell_Guida_1.0.pdf) · [Guida funzionale](magoh_spell/README.md) · [Note della versione](magoh_spell/CHANGELOG.md)

## Licenza e risorse

Il codice originale è distribuito con licenza **GPL-2.0-or-later**. Il template GNA incluso proviene dalla pagina download del Geoportale Nazionale per l’Archeologia ed è adattato per l’uso nel plugin. Le illustrazioni dei cappelli sono di Vittoria Bellon. Le attribuzioni di dati e risorse di terzi sono riportate separatamente. [Licenza](LICENSE) · [Attribuzioni](magoh_spell/ATTRIBUZIONI.md) · [Fonte del Template GNA](https://gna.cultura.gov.it/download.html)

## Creare lo ZIP installabile

Dalla radice del repository esegui `python tools/package_plugin.py`. Troverai l’archivio in `dist/MAGOH_Spell_V1.0.zip`. È il file da allegare alla release su GitHub.
