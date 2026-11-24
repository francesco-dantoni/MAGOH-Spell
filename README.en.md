<p align="center">
  <img src="magoh_spell/icons/hat_m.png" width="160" alt="MAGOH Spell hat">
</p>

<h1 align="center">MAGOH Spell</h1>

<p align="center"><a href="README.md">Italiano</a> · <b>English</b></p>

<p align="center">
  <a href="https://github.com/francesco-dantoni/MAGOH-Spell/releases/latest"><img src="https://img.shields.io/badge/Download_the_plugin-MAGOH__Spell__V1.0.zip-1e78b4?style=for-the-badge" alt="Download the plugin: MAGOH_Spell_V1.0.zip"></a>
</p>

MAGOH Spell is a QGIS plugin for recording and consulting archaeological interventions, finds and evidence according to the MAGOH data model. It lets you work in a local GeoPackage, import data from the MAGOH APIs, produce PDF record sheets and convert the data into the GNA 1.6.0 Template of the Italian National Geoportal for Archaeology (Geoportale Nazionale per l’Archeologia).

Developed by **Francesco D’Antoni** and **Lorena Bravi**.

## MAGOH and MAGOH Wiki

- **[MAGOH](https://magoh.cfs.unipi.it/web/guest)** (*Managing Archaeological data for a sustainable Governance of Heritage*) is the web portal for consulting archaeological data developed by MAPPA Lab, Department of Civilisations and Forms of Knowledge, University of Pisa. MAGOH Spell imports its records through the APIs.
- **[MAGOH Wiki](https://magoh-wiki.cfs.unipi.it/en/index.html)** is the technical documentation of the data model: Intervention record, Find (3rd level), Archaeological evidence (4th level), burials and thesauri. The plugin forms open the relevant Wiki page directly. It is also available [in Italian](https://magoh-wiki.cfs.unipi.it/index.html).

## Installing in QGIS

> **Note:** to install the plugin, use the release ZIP, not the green **Code → Download ZIP** button or “Source code”. Those archives contain the source code of the whole repository and QGIS does not recognise them as a plugin.

1. Click **Download the plugin** above, or open the **Releases** section, and download the `MAGOH_Spell_V1.0.zip` attachment.
2. In QGIS open **Plugins → Manage and Install Plugins → Install from ZIP** and select the archive without extracting it.
3. Enable **MAGOH Spell** and open **Plugins → MAGOH Spell**. From **Project and data** you can create a new project or open an existing one.

Before updating a project already in use, save your edits and make a copy of the GeoPackage. The metadata declare compatibility from QGIS 3.34 to 4.x. The plugin interface is available in Italian and English.

## Working with records

- Browse the links between interventions, finds and evidence. In a project with the prefix LSF, the local series start at `LSF-1`, `LSFR-1` and `LSFE-1`. Imported records show their MAGOH identifier.
- Search for a record by attribute, ID or vocabulary term. The **Chronology and categories** panel lets you explore chronological ranges and characterisation levels.
- Export PDF record sheets or generate the GNA project in the folder with the `_GNA` suffix. The conversion log traces each GNA record back to its source records.

[Illustrated guide (PDF, in Italian)](docs/MAGOH_Spell_Guida_1.0.pdf) · [Functional guide (in Italian)](magoh_spell/README.md) · [Release notes (in Italian)](magoh_spell/CHANGELOG.md)

## Licence and resources

The original code is released under the **GPL-2.0-or-later** licence. The bundled GNA template comes from the download page of the National Geoportal for Archaeology and has been adapted for use in the plugin. The hat illustrations are by Vittoria Bellon. Attributions for third-party data and resources are listed separately. [Licence](LICENSE) · [Attributions (in Italian)](magoh_spell/ATTRIBUZIONI.md) · [GNA Template source](https://gna.cultura.gov.it/download.html)

## Building the installable ZIP

From the repository root run `python tools/package_plugin.py`. The archive is written to `dist/MAGOH_Spell_V1.0.zip`. This is the file to attach to the GitHub release.
