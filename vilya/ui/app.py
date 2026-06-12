"""Vilya cast picker: a small Qt tray app (Super+K summons it).

Deliberately a thin shell over the CLI: scanning and connecting run the
`vilya` commands as subprocesses, so the protocol machinery stays in one
place and the UI can be swapped/themed/ported freely. Qt inherits the
system theme (Breeze Twilight on this machine) with zero styling code;
anything custom should go through QSS later, keeping the door open for
other desktops.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QRadioButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..modes import MODES

SOCKET_NAME = "vilya-ui"
SCAN_SECONDS = 6


def _vilya_cmd() -> list[str]:
    # Use the dedicated interpreter when present (KWin whitelist for
    # native-size virtual outputs); fall back to the current one.
    exe = os.path.join(os.path.dirname(sys.executable), "vilya-python")
    if not os.path.exists(exe):
        exe = sys.executable
    return [exe, "-m", "vilya"]


class CastWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Vilya — Cast")
        self.setWindowFlag(Qt.WindowType.Dialog)
        self.setFixedWidth(380)

        self._scan_proc: QProcess | None = None
        self._connect_proc: QProcess | None = None
        self._streaming = False

        layout = QVBoxLayout(self)

        self.status = QLabel("Scanning for displays…")
        layout.addWidget(self.status)

        self.devices = QListWidget()
        self.devices.itemDoubleClicked.connect(lambda _: self._connect())
        self.devices.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.devices)

        row = QHBoxLayout()
        row.addWidget(QLabel("Display:"))
        self.mirror = QRadioButton("Mirror")
        self.extend = QRadioButton("Extend")
        self.extend.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.mirror)
        group.addButton(self.extend)
        self.mirror.toggled.connect(self._default_mode)
        row.addWidget(self.mirror)
        row.addWidget(self.extend)
        row.addStretch()
        layout.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Resolution:"))
        self.mode = QComboBox()
        for name in sorted(MODES):
            self.mode.addItem(name)
        row2.addWidget(self.mode, stretch=1)
        layout.addLayout(row2)
        self._default_mode()

        row3 = QHBoxLayout()
        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.clicked.connect(self.rescan)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setDefault(True)
        self.connect_btn.clicked.connect(self._connect_or_disconnect)
        row3.addWidget(self.rescan_btn)
        row3.addStretch()
        row3.addWidget(self.connect_btn)
        layout.addLayout(row3)

        self._update_buttons()

    # -- scanning -------------------------------------------------------

    def rescan(self) -> None:
        if self._scan_proc or self._streaming:
            return
        self.devices.clear()
        self.status.setText("Scanning for displays…")
        self.rescan_btn.setEnabled(False)
        proc = QProcess(self)
        proc.finished.connect(self._scan_done)
        proc.start(
            _vilya_cmd()[0],
            _vilya_cmd()[1:]
            + ["scan", "--time", str(SCAN_SECONDS), "--porcelain"],
        )
        self._scan_proc = proc

    def _scan_done(self) -> None:
        proc, self._scan_proc = self._scan_proc, None
        self.rescan_btn.setEnabled(True)
        out = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        found = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            name, _addr, wfd = parts
            item = QListWidgetItem(
                QIcon.fromTheme(
                    "video-display" if wfd == "wfd" else "network-wireless"
                ),
                name,
            )
            if wfd != "wfd":
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.devices.addItem(item)
            found += 1
        self.status.setText(
            f"Found {found} device(s)" if found else
            "No displays found — is Second Screen on?"
        )
        if found:
            for i in range(self.devices.count()):
                if self.devices.item(i).flags() & Qt.ItemFlag.ItemIsEnabled:
                    self.devices.setCurrentRow(i)
                    break
        self._update_buttons()

    # -- connecting -----------------------------------------------------

    def _default_mode(self) -> None:
        self.mode.setCurrentText(
            "1080p30" if self.mirror.isChecked() else "1200p30"
        )

    def _connect_or_disconnect(self) -> None:
        if self._streaming or self._connect_proc:
            self.disconnect_cast()
        else:
            self._connect()

    def _connect(self) -> None:
        item = self.devices.currentItem()
        if not item or self._connect_proc:
            return
        display = "mirror" if self.mirror.isChecked() else "extend"
        args = _vilya_cmd()[1:] + [
            "connect",
            "--peer", item.text(),
            "--display", display,
            "--mode", self.mode.currentText(),
        ]
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(self._connect_output)
        proc.finished.connect(self._connect_finished)
        proc.start(_vilya_cmd()[0], args)
        self._connect_proc = proc
        self.status.setText(f"Connecting to {item.text()}…")
        self.connect_btn.setText("Cancel")
        self.rescan_btn.setEnabled(False)

    def _connect_output(self) -> None:
        if not self._connect_proc:
            return
        out = bytes(self._connect_proc.readAllStandardOutput()).decode(
            errors="replace"
        )
        if "RTSP state: STREAMING" in out:
            self._streaming = True
            self.status.setText("Casting ✓")
            self.connect_btn.setText("Disconnect")
            self.hide()  # job done; lives on in the tray

    def _connect_finished(self) -> None:
        self._connect_proc = None
        was_streaming = self._streaming
        self._streaming = False
        self.connect_btn.setText("Connect")
        self.rescan_btn.setEnabled(True)
        self.status.setText(
            "Disconnected" if was_streaming else "Could not connect"
        )
        self._update_buttons()

    def disconnect_cast(self) -> None:
        if self._connect_proc:
            self._connect_proc.terminate()  # vilya tears down on SIGTERM

    def _update_buttons(self) -> None:
        self.connect_btn.setEnabled(
            self._streaming
            or self._connect_proc is not None
            or self.devices.currentItem() is not None
        )

    @property
    def streaming(self) -> bool:
        return self._streaming

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        if not self._streaming and not self._scan_proc:
            QTimer.singleShot(0, self.rescan)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("vilya")
    app.setQuitOnLastWindowClosed(False)

    # Single instance: second launch tells the first to show itself.
    probe = QLocalSocket()
    probe.connectToServer(SOCKET_NAME)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(SOCKET_NAME)
    server.listen(SOCKET_NAME)

    window = CastWindow()
    server.newConnection.connect(
        lambda: (window.show(), window.raise_(), window.activateWindow())
    )

    tray = QSystemTrayIcon(QIcon.fromTheme("video-display"), app)
    tray.setToolTip("Vilya — cast to a wireless display")
    menu = QMenu()
    act_show = QAction("Cast…")
    act_show.triggered.connect(lambda: (window.show(), window.raise_()))
    act_disconnect = QAction("Disconnect")
    act_disconnect.triggered.connect(window.disconnect_cast)
    act_quit = QAction("Quit")

    def _quit() -> None:
        window.disconnect_cast()
        QTimer.singleShot(800, app.quit)

    act_quit.triggered.connect(_quit)
    menu.addAction(act_show)
    menu.addAction(act_disconnect)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: (window.show(), window.raise_())
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    tray.show()

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
