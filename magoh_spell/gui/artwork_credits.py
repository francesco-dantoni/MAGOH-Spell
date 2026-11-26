"""Small local attribution dialog; the artist's image is never modified.

clearer layout (the illustration itself, the artist, a clickable
Instagram handle and a rights note). The PNG is only displayed, never edited.
"""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDialog, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QFrame
from ..ui_theme import dialog_frame, apply_style, set_role, artwork_pixmap, PETROL, MUTED, INK

INSTAGRAM_HANDLE = '@icestorming'
INSTAGRAM_URL = 'https://www.instagram.com/icestorming/'


class ArtworkCreditsDialog(QDialog):
    def __init__(self, parent, language='it'):
        super().__init__(parent)
        self.setObjectName('MagohArtworkCredits')
        self.setModal(False)
        body, footer = dialog_frame(self, '', scroll=False)

        row = QHBoxLayout()
        row.setSpacing(18)
        self.picture = QLabel(self)
        self.picture.setObjectName('MagohArtworkPicture')
        self.picture.setPixmap(artwork_pixmap(112))
        self.picture.setFixedSize(120, 120)
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.picture, 0, Qt.AlignmentFlag.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(4)
        self.role = QLabel(self)
        self.role.setObjectName('MagohHint')
        self.artist = QLabel('Vittoria Bellon', self)
        self.artist.setObjectName('MagohArtworkArtist')
        self.artist.setStyleSheet('font-size: 15pt; font-weight: 600; color: %s;' % INK)
        self.artist.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.handle = QLabel(self)
        self.handle.setObjectName('MagohArtworkHandle')
        self.handle.setTextFormat(Qt.TextFormat.RichText)
        self.handle.setText('<a href="%s" style="color:%s; text-decoration:underline; font-weight:600;">%s</a>'
                            % (INSTAGRAM_URL, PETROL, INSTAGRAM_HANDLE))
        self.handle.setOpenExternalLinks(True)
        self.handle.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.handle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.handle.setToolTip(INSTAGRAM_URL)
        for widget in (self.role, self.artist, self.handle):
            text.addWidget(widget)
        text.addStretch(1)
        row.addLayout(text, 1)
        body.addLayout(row)

        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet('color: #dfe5e7;')
        body.addWidget(line)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        self.note.setStyleSheet('color: %s;' % MUTED)
        body.addWidget(self.note)

        self.close_button = QPushButton(self)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        set_role(self.close_button, primary=True)
        footer.addStretch(1)
        footer.addWidget(self.close_button)
        apply_style(self)
        self.retranslate(language)
        self.setMinimumWidth(420)
        self.adjustSize()

    def retranslate(self, language):
        english = language == 'en'
        title = 'Image credits' if english else 'Crediti dell’immagine'
        self.setWindowTitle(title)
        self.magoh_header.title_label.setText(title)
        self.role.setText('Illustration of the MAGOH Spell hat' if english else 'Illustrazione del cappello di MAGOH Spell')
        self.handle.setAccessibleName(('Instagram profile ' if english else 'Profilo Instagram ') + INSTAGRAM_HANDLE)
        self.note.setText('The illustration is used as the plugin icon without changes. All rights remain with the artist.'
                          if english else
                          'L’illustrazione è usata come icona del plugin senza modifiche. Tutti i diritti restano all’artista.')
        self.close_button.setText('Close' if english else 'Chiudi')
