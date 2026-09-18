import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "bstail.omatranslate"
  ipcTarget: "bstail.omatranslate"
  manageIpc: false

  property var anchorItem: null
  property bool openedFromHotkey: false
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  // Label shown on the bar icon: the language glyph is static; the panel
  // keeps the widget visible at all times.
  readonly property string label: "\uf1ab"

  // --- live state (mirrors the controller's /status) ----------------------
  property bool serviceRunning: false
  property string direction: "en-es"
  property bool autoSpeak: false
  property string outputDestination: "speakers"
  property bool incomingEnabled: true
  property string incomingDirection: "es-en"
  property bool multimedia: false
  property bool twoTier: false
  property bool gateEnabled: true
  property real gateOpenRms: 200.0
  property bool history: true
  property bool glossaryEnabled: false
  property var glossaryPhrases: []
  property real glossaryBoost: 3.0
  property bool activationEnabled: false
  property string activationClass: ""
  property string activationTitle: ""
  property bool debugCapture: true
  property bool preprocess: true
  property real preampDb: 6.0
  property bool keepAwake: true
  property bool clipboardLogText: false

  // Theme tokens. `Color.accent` and `Color.urgent` already track the active
  // theme (the shell applies theme changes automatically); `Color.urgent` is
  // the theme's red. Green/cyan aren't exposed by the shell palette, so they
  // are read once from the current theme's colors.toml via FileView (the same
  // mechanism the shell's own Color singleton uses).
  readonly property color themeAccent: Color.accent
  readonly property color themeRed: Color.urgent
  property color themeGreen: "#29D398"
  property color themeCyan: "#59E1E3"

  // Plugin-local glow geometry for the header start/stop button (not a theme
  // token). The halo extends this far past the button on every side, so the
  // header row and right margin below reserve room for it. Core QtQuick only
  // (no FastBlur / external module) so Quickshell can load it.
  readonly property real glowSpread: 10

  // Looping 0..1..0 phase that drives the halo pulse. A single ping-pong
  // NumberAnimation on a plain property is guaranteed to oscillate every
  // cycle (unlike re-evaluating `from:` inside a looping SequentialAnimation).
  property real glowPhase: 0

  NumberAnimation on glowPhase {
    from: 0.0
    to: 1.0
    duration: 1400
    easing.type: Easing.InOutSine
    loops: Animation.Infinite
    running: root.opened
  }

  FileView {
    id: themeColors
    path: Quickshell.env("HOME") + "/.local/state/omarchy/current/theme/colors.toml"
    watchChanges: false
    printErrors: false
    onLoaded: {
      var raw = String(text() || "")
      var m = raw.match(/^\s*green\s*=\s*["']?(#[0-9A-Fa-f]{6})/m)
      if (m) root.themeGreen = m[1]
      var c = raw.match(/^\s*cyan\s*=\s*["']?(#[0-9A-Fa-f]{6})/m)
      if (c) root.themeCyan = c[1]
    }
  }

  property bool pending: false

  function open() {
    openedFromHotkey = false
    setCenterHoverRevealSuppressed(false)
    root.controller.show()
    root.refreshStatus()
  }

  function openFromHotkey() {
    openedFromHotkey = true
    root.controller.show()
    root.refreshStatus()
    Qt.callLater(function() {
      if (root.opened) setCenterHoverRevealSuppressed(true)
    })
  }

  function close() {
    setCenterHoverRevealSuppressed(false)
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.openFromHotkey()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function setCenterHoverRevealSuppressed(value) {
    if (root.bar && "_centerHoverRevealSuppressed" in root.bar)
      root.bar._centerHoverRevealSuppressed = value
  }

  // --- controller bridge --------------------------------------------------

  function refreshStatus() {
    statusProbe.running = false
    statusProbe.running = true
  }

  function applySettings(settings) {
    var msg = { "action": "set", "settings": settings }
    ctlProc.payload = JSON.stringify(msg)
    ctlProc.running = false
    ctlProc.running = true
  }

  function toggleService() {
    var action = root.serviceRunning ? "stop" : "start"
    svcProc.action = action
    svcProc.running = false
    svcProc.running = true
  }

  function bumpGate(delta) {
    var v = Math.round(root.gateOpenRms) + delta
    if (v < 50) v = 50
    if (v > 2000) v = 2000
    root.gateOpenRms = v
    gateField.text = String(v)
    root.applySettings({ "gate": { "open_rms": v } })
  }

  function copyDiagnostics() {
    root.bar.run("omarchy-launch-floating-terminal-with-presentation olt-ctl diagnostics")
  }

  function clearLogs() {
    clearProc.running = false
    clearProc.running = true
  }

  Process {
    id: statusProbe
    command: ["curl", "-fsS", "--max-time", "3", "http://127.0.0.1:8670/status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var d = JSON.parse(String(text || "{}"))
          root.serviceRunning = true
          root.direction = d.direction || "en-es"
          root.autoSpeak = !!d.auto_speak
          root.outputDestination = d.output_destination || "speakers"
          root.incomingEnabled = !!d.incoming_enabled
          root.incomingDirection = d.incoming_direction || "es-en"
          root.multimedia = !!d.multimedia
          root.twoTier = !!d.two_tier
          if (d.gate) {
            root.gateEnabled = d.gate.enabled !== false
            root.gateOpenRms = Number(d.gate.open_rms) || 200.0
          }
          root.history = d.history !== false
          if (d.glossary) {
            root.glossaryEnabled = !!d.glossary.enabled
            root.glossaryPhrases = d.glossary.phrases || []
            root.glossaryBoost = Number(d.glossary.boost) || 3.0
          }
          if (d.activation) {
            root.activationEnabled = !!d.activation.enabled
            root.activationClass = d.activation.app_class || ""
            root.activationTitle = d.activation.app_title || ""
          }
          root.debugCapture = d.debug_capture !== false
          root.preprocess = d.preprocess_enable !== false
          root.preampDb = Number(d.preamp_db) || 6.0
          root.keepAwake = d.keep_awake !== false
          root.clipboardLogText = d.clipboard_log_text === true
        } catch (e) {
          root.serviceRunning = false
        }
      }
    }
    onExited: if (exitCode !== 0) root.serviceRunning = false
  }

  Process {
    id: ctlProc
    property string payload: ""
    command: ["curl", "-fsS", "--max-time", "5",
              "-X", "POST",
              "-H", "Content-Type: application/json",
              "-d", ctlProc.payload,
              "http://127.0.0.1:8670/ctl"]
    onExited: Qt.callLater(root.refreshStatus)
  }

  Process {
    id: svcProc
    property string action: ""
    command: ["systemctl", "--user", svcProc.action,
              "omatranslate.service", "libretranslate-live.service"]
    onExited: Qt.callLater(root.refreshStatus)
  }

  Process {
    id: clearProc
    command: ["olt-ctl", "clear_logs"]
    onExited: Qt.callLater(root.refreshStatus)
  }

  Timer {
    interval: 4000
    running: root.opened
    repeat: true
    onTriggered: root.refreshStatus()
  }

  // --- panel --------------------------------------------------------------

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(520))
    contentHeight: panel.fittedContentHeight(settingsColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

        Flickable {
          id: settingsScroll
          anchors.fill: parent
          contentWidth: width
          contentHeight: settingsColumn.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          interactive: contentHeight > height

          Rectangle {
            anchors.fill: parent
            color: Color.popups.background
          }

          Column {
          id: settingsColumn
          width: settingsScroll.width
          spacing: Style.space(20)

          // ---- header ----------------------------------------------------
          Item {
            width: parent.width
            // Tall enough that the glow halo (glowSpread past the button on every
            // side) stays inside the Flickable and never clips at the panel edge.
            height: Style.space(56)

            Text {
              id: headerGlyph
              text: "\uf1ab"
              color: root.serviceRunning ? Style.hoverStateColor(root.bar.foreground, Color.accent) : Qt.darker(root.bar.foreground, 1.5)
              font.family: root.bar.fontFamily
              font.pixelSize: Math.round(Style.font.heading * 2)
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: headerText
              anchors.left: headerGlyph.right
              anchors.leftMargin: Style.space(12)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.xs

              Text {
                text: "OmaTranslate"
                color: root.themeAccent
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.heading
                font.bold: true
              }

              Text {
                text: root.serviceRunning ? "Running" : "Stopped"
                color: root.serviceRunning ? root.themeGreen : root.themeRed
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }
            }

            // Accent glow halo behind the start/stop button: a smooth Gaussian-like
            // falloff from concentric rings (core QtQuick only — no Mask/FastBlur /
            // external module, which Quickshell can't load). The sharp Button
            // renders once on top.
            Item {
              id: headerButtonWrap
              width: headerButton.implicitWidth + root.glowSpread * 2
              height: headerButton.implicitHeight + root.glowSpread * 2
              anchors.right: parent.right
              // Keeps the button's right edge where it was before the halo.
              anchors.rightMargin: Math.max(0, Style.space(10) - root.glowSpread)
              anchors.verticalCenter: parent.verticalCenter

              // Halo from many concentric rounded rings whose opacity falls off
              // smoothly (Gaussian-like) — reads as one soft glow, not bands.
              // Core QtQuick only (no Mask/FastBlur/external module). The pulse is
              // driven by root.glowPhase so every ring breathes each cycle.
              Repeater {
                model: 24

                Rectangle {
                  required property int index
                  anchors.centerIn: parent
                  readonly property real t: index / (24 - 1)   // 0 = innermost, 1 = outermost
                  width: headerButton.implicitWidth + root.glowSpread * 2 * t
                  height: headerButton.implicitHeight + root.glowSpread * 2 * t
                  radius: Style.cornerRadius + root.glowSpread * t
                  color: root.themeAccent
                  // Smooth falloff (bright at the button, ~0 at the rim) x pulse.
                  // Pulse floor is 0 so the glow fully fades out each cycle.
                  opacity: 0.046875 * Math.exp(-4.0 * t * t) * root.glowPhase
                }
              }

              Button {
                id: headerButton
                anchors.centerIn: parent
                text: root.serviceRunning ? "Stop" : "Start"
                iconText: "\uf011"
                bordered: true
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: root.toggleService()
              }
            }
          }

          // ---- outgoing --------------------------------------------------
          Rectangle {
            width: parent.width
            implicitHeight: outgoingCol.implicitHeight + Style.space(12) * 2
            radius: Style.cornerRadius
            color: Qt.rgba(root.themeAccent.r, root.themeAccent.g, root.themeAccent.b, 0.04)
            border.color: Qt.rgba(root.themeAccent.r, root.themeAccent.g, root.themeAccent.b, 0.35)
            border.width: 1

            Column {
              id: outgoingCol
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.bottom: parent.bottom
              anchors.margins: Style.space(12)
              spacing: Style.space(8)

              PanelSectionHeader { text: "OUTGOING"; foreground: root.themeAccent; fontSize: Style.font.body }

              ButtonGroup {
                id: directionGroup
                width: parent.width
                foreground: root.bar.foreground
                background: Color.popups.background
                accent: root.themeAccent
                fontFamily: root.bar.fontFamily
                value: root.direction
                options: [
                  { value: "en-es", label: "EN → ES", tooltip: "You speak English, it speaks Spanish" },
                  { value: "es-en", label: "ES → EN", tooltip: "You speak Spanish, it speaks English" }
                ]
                onChanged: function(v) { root.direction = v; root.applySettings({ "direction": v }) }
              }

              Toggle {
                width: parent.width
                label: "Auto-speak"
                description: "Speak the translation automatically after you release F10"
                checked: root.autoSpeak
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.autoSpeak = !root.autoSpeak
                  root.applySettings({ "auto_speak": root.autoSpeak })
                }
              }

              Toggle {
                width: parent.width
                label: "Speak through speakers"
                description: "ON: play translation on your speakers (testing). OFF: route to the virtual microphone (calls)."
                checked: root.outputDestination === "speakers"
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.outputDestination = (root.outputDestination === "speakers") ? "virtual_mic" : "speakers"
                  root.applySettings({ "output_destination": root.outputDestination })
                }
              }
            }
          }

          // ---- incoming --------------------------------------------------
          Rectangle {
            width: parent.width
            implicitHeight: incomingCol.implicitHeight + Style.space(12) * 2
            radius: Style.cornerRadius
            color: Qt.rgba(root.themeCyan.r, root.themeCyan.g, root.themeCyan.b, 0.04)
            border.color: Qt.rgba(root.themeCyan.r, root.themeCyan.g, root.themeCyan.b, 0.35)
            border.width: 1

            Column {
              id: incomingCol
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.bottom: parent.bottom
              anchors.margins: Style.space(12)
              spacing: Style.space(8)

              PanelSectionHeader { text: "INCOMING"; foreground: root.themeCyan; fontSize: Style.font.body }

              Toggle {
                width: parent.width
                label: "Listen for incoming speech"
                description: "Transcribe the other side of the call (holds the mic open)"
                checked: root.incomingEnabled
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.incomingEnabled = !root.incomingEnabled
                  root.applySettings({ "incoming_enabled": root.incomingEnabled })
                }
              }

              ButtonGroup {
                id: incomingDirectionGroup
                width: parent.width
                foreground: root.bar.foreground
                background: Color.popups.background
                accent: root.themeCyan
                fontFamily: root.bar.fontFamily
                value: root.incomingDirection
                options: [
                  { value: "es-en", label: "ES → EN", tooltip: "The other person speaks Spanish" },
                  { value: "en-es", label: "EN → ES", tooltip: "The other person speaks English" }
                ]
                onChanged: function(v) {
                  root.incomingDirection = v
                  root.applySettings({ "incoming_direction": v })
                }
              }

              Toggle {
                width: parent.width
                label: "Multimedia mode"
                description: "For videos/voice messages: longer end-of-utterance window so continuous speech is segmented into cards"
                checked: root.multimedia
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.multimedia = !root.multimedia
                  root.applySettings({ "multimedia": root.multimedia })
                }
              }

              Toggle {
                width: parent.width
                visible: root.multimedia
                label: "Ignore silence"
                description: "Mute the monitor while it stays quiet so idle speaker noise can't produce phantom translations"
                checked: root.gateEnabled
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.gateEnabled = !root.gateEnabled
                  root.applySettings({ "gate": { "enabled": root.gateEnabled } })
                }
              }

              Column {
                width: parent.width
                spacing: Style.space(6)
                visible: root.multimedia

                Text {
                  width: parent.width
                  text: "Minimum speech level"
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.body
                }

                Row {
                  id: gateRow
                  width: parent.width
                  spacing: Style.space(10)

                  Button {
                    id: gateDown
                    text: "−"
                    iconText: ""
                    bordered: true
                    foreground: root.bar.foreground
                    accent: Color.accent
                    fontFamily: root.bar.fontFamily
                    onClicked: root.bumpGate(-20)
                  }

                  TextField {
                    id: gateField
                    width: parent.width - gateDown.implicitWidth - gateUp.implicitWidth - Style.space(10) * 2
                    text: String(Math.round(root.gateOpenRms))
                    foreground: root.bar.foreground
                    accent: Color.accent
                    font.family: root.bar.fontFamily
                    onAccepted: {
                      var v = Number(text.replace(/[^0-9]/g, ""))
                      if (isNaN(v) || v <= 0) { text = String(Math.round(root.gateOpenRms)); return }
                      root.gateOpenRms = v
                      root.applySettings({ "gate": { "open_rms": v } })
                    }
                  }

                  Button {
                    id: gateUp
                    text: "+"
                    iconText: ""
                    bordered: true
                    foreground: root.bar.foreground
                    accent: Color.accent
                    fontFamily: root.bar.fontFamily
                    onClicked: root.bumpGate(20)
                  }
                }

                Text {
                  width: parent.width
                  text: "Suggested range 50–2000 (default 200). Higher = only louder audio is translated. Lower = more sensitive to quiet speech."
                  color: Qt.darker(root.bar.foreground, 1.5)
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }
              }

              Toggle {
                width: parent.width
                label: "Two-tier accuracy"
                description: "Re-transcribe each finished phrase with a more accurate offline model (Parakeet) and replace the text in place"
                checked: root.twoTier
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.twoTier = !root.twoTier
                  root.applySettings({ "two_tier": root.twoTier })
                }
              }

              Toggle {
                width: parent.width
                label: "Translation history"
                description: "ON: keep all cards (scrollable). OFF: show only the newest card."
                checked: root.history
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.history = !root.history
                  root.applySettings({ "history": root.history })
                }
              }
            }
          }

          // ---- glossary --------------------------------------------------
          Rectangle {
            width: parent.width
            implicitHeight: glossaryCol.implicitHeight + Style.space(12) * 2
            radius: Style.cornerRadius
            color: Qt.rgba(root.themeGreen.r, root.themeGreen.g, root.themeGreen.b, 0.04)
            border.color: Qt.rgba(root.themeGreen.r, root.themeGreen.g, root.themeGreen.b, 0.35)
            border.width: 1

            Column {
              id: glossaryCol
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.bottom: parent.bottom
              anchors.margins: Style.space(12)
              spacing: Style.space(8)

              PanelSectionHeader { text: "GLOSSARY"; foreground: root.themeGreen; fontSize: Style.font.body }

              Toggle {
                width: parent.width
                label: "Word boosting"
                description: "Bias recognition toward names and terms you add below"
                checked: root.glossaryEnabled
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.glossaryEnabled = !root.glossaryEnabled
                  root.applySettings({ "glossary": { "enabled": root.glossaryEnabled, "phrases": root.glossaryPhrases, "boost": root.glossaryBoost } })
                }
              }

              TextField {
                id: glossaryField
                width: parent.width
                placeholderText: "Add a term, press Enter (e.g. Kowalczyk)"
                foreground: root.bar.foreground
                accent: Color.accent
                font.family: root.bar.fontFamily
                onAccepted: {
                  var t = text.trim()
                  if (t === "") return
                  var list = root.glossaryPhrases.slice()
                  if (list.indexOf(t) < 0) list.push(t)
                  root.glossaryPhrases = list
                  text = ""
                  root.applySettings({ "glossary": { "enabled": root.glossaryEnabled, "phrases": root.glossaryPhrases, "boost": root.glossaryBoost } })
                }
              }

              Flow {
                width: parent.width
                spacing: Style.space(6)
                visible: root.glossaryPhrases.length > 0

                Repeater {
                  model: root.glossaryPhrases

                  Rectangle {
                    required property var modelData
                    width: chipRow.implicitWidth + Style.space(16)
                    height: Style.space(26)
                    radius: Style.cornerRadius
                    color: Style.hoverFillFor(root.bar.foreground, Color.accent)

                    Row {
                      id: chipRow
                      anchors.centerIn: parent
                      spacing: Style.space(8)

                      Text {
                        text: modelData
                        color: root.bar.foreground
                        font.family: root.bar.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        anchors.verticalCenter: parent.verticalCenter
                      }

                      Text {
                        text: "✕"
                        color: Qt.darker(root.bar.foreground, 1.4)
                        font.family: root.bar.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        anchors.verticalCenter: parent.verticalCenter

                        MouseArea {
                          anchors.fill: parent
                          cursorShape: Qt.PointingHandCursor
                          onClicked: {
                            var list = root.glossaryPhrases.slice()
                            var i = list.indexOf(modelData)
                            if (i >= 0) list.splice(i, 1)
                            root.glossaryPhrases = list
                            root.applySettings({ "glossary": { "enabled": root.glossaryEnabled, "phrases": root.glossaryPhrases, "boost": root.glossaryBoost } })
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }

          // ---- activation -------------------------------------------------
          Rectangle {
            width: parent.width
            implicitHeight: activationCol.implicitHeight + Style.space(12) * 2
            radius: Style.cornerRadius
            color: Qt.rgba(root.themeAccent.r, root.themeAccent.g, root.themeAccent.b, 0.04)
            border.color: Qt.rgba(root.themeAccent.r, root.themeAccent.g, root.themeAccent.b, 0.35)
            border.width: 1

            Column {
              id: activationCol
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.bottom: parent.bottom
              anchors.margins: Style.space(12)
              spacing: Style.space(8)

              PanelSectionHeader { text: "ACTIVATION"; foreground: root.themeAccent; fontSize: Style.font.body }

              Toggle {
                width: parent.width
                label: "Only translate in the call app"
                description: "Pause when the focused window isn't your call app"
                checked: root.activationEnabled
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.activationEnabled = !root.activationEnabled
                  root.applySettings({ "activation": { "enabled": root.activationEnabled, "app_class": root.activationClass, "app_title": root.activationTitle } })
                }
              }

              TextField {
                id: activationField
                width: parent.width
                placeholderText: "Window class (e.g. zoom, teams, discord)"
                text: root.activationClass
                foreground: root.bar.foreground
                accent: Color.accent
                font.family: root.bar.fontFamily
                onEditingFinished: {
                  root.activationClass = text.trim()
                  root.applySettings({ "activation": { "enabled": root.activationEnabled, "app_class": root.activationClass, "app_title": root.activationTitle } })
                }
              }
            }
          }

          // ---- diagnostics ------------------------------------------------
          Rectangle {
            width: parent.width
            implicitHeight: diagCol.implicitHeight + Style.space(12) * 2
            radius: Style.cornerRadius
            color: Qt.rgba(root.themeGreen.r, root.themeGreen.g, root.themeGreen.b, 0.04)
            border.color: Qt.rgba(root.themeGreen.r, root.themeGreen.g, root.themeGreen.b, 0.35)
            border.width: 1

            Column {
              id: diagCol
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.bottom: parent.bottom
              anchors.margins: Style.space(12)
              spacing: Style.space(8)

              PanelSectionHeader { text: "DIAGNOSTICS"; foreground: root.themeGreen; fontSize: Style.font.body }

              Toggle {
                width: parent.width
                label: "Save all audio transcripts"
                description: "Store full transcript text and audio in logs/WAVs (for debugging). OFF keeps only timing metadata for privacy."
                checked: root.debugCapture
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.debugCapture = !root.debugCapture
                  root.applySettings({ "debug_capture": root.debugCapture })
                }
              }

              Toggle {
                width: parent.width
                label: "Log clipboard text"
                description: "Include clipboard source and translation text in logs. OFF logs only character counts for privacy."
                checked: root.clipboardLogText
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.clipboardLogText = !root.clipboardLogText
                  root.applySettings({ "clipboard_log_text": root.clipboardLogText })
                }
              }

              Toggle {
                width: parent.width
                label: "Audio cleanup"
                description: "High-pass filter + gain boost before recognition (helps quiet or muffled audio)"
                checked: root.preprocess
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.preprocess = !root.preprocess
                  root.applySettings({ "preprocess_enable": root.preprocess })
                }
              }

              Toggle {
                width: parent.width
                label: "Keep screen awake"
                description: "Suppress the screensaver and lock while the translator is running"
                checked: root.keepAwake
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: {
                  root.keepAwake = !root.keepAwake
                  root.applySettings({ "keep_awake": root.keepAwake })
                }
              }

              Button {
                width: parent.width
                text: "Open diagnostics"
                iconText: "\uf188"
                bordered: true
                foreground: root.bar.foreground
                accent: Color.accent
                fontFamily: root.bar.fontFamily
                onClicked: root.copyDiagnostics()
              }

              Text {
                width: parent.width
                text: "Opens a terminal with service state, controller status, recent logs, and engine info."
                color: Qt.darker(root.bar.foreground, 1.5)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }

              Button {
                width: parent.width
                text: "Clear logs & history"
                iconText: "\uf1f8"
                bordered: true
                foreground: Color.urgent
                accent: Color.urgent
                fontFamily: root.bar.fontFamily
                onClicked: root.clearLogs()
              }

              Text {
                width: parent.width
                text: "Deletes all log files, translation history, debug captures, and clears the overlay."
                color: Qt.darker(root.bar.foreground, 1.5)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }
          }
        }

        // Speed up trackpad scrolling 3x (core QtQuick only). Declared after the
        // Column so it sits on top and receives wheel events first; no accepted
        // buttons means clicks pass through to the controls below.
        MouseArea {
          anchors.fill: parent
          acceptedButtons: Qt.NoButton
          hoverEnabled: true
          onWheel: {
            var max = Math.max(0, settingsScroll.contentHeight - settingsScroll.height)
            settingsScroll.contentY = Math.max(0, Math.min(max, settingsScroll.contentY - wheel.angleDelta.y * 1.5))
          }
        }
      }
    }
  }
}
