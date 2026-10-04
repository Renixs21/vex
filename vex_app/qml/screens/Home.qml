import QtQuick
import QtQuick.Controls.Basic
import "../components"

// Home: the live orb, a greeting, the input bar and quick chips; Today and Activity on the right; response time, health and budget
// tiles at the bottom. A confirmation in progress shows as a banner: it is answered on Vex's own screen, never here.
FocusScope {
    id: root
    signal navigate(string screen)
    readonly property bool narrow: width < 900

    function greeting() {
        const h = new Date().getHours()
        return h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening"
    }
    function fmt(v, unit) { return (v === undefined || v === null) ? "—" : Number(v).toFixed(2) + unit }

    // ---- header -----------------------------------------------------------------------------------------------------------------
    Item {
        id: header
        anchors { left: parent.left; right: parent.right; top: parent.top }
        height: 52
        Row {
            anchors.verticalCenter: parent.verticalCenter
            spacing: 10
            Rectangle {                                       // status chip
                height: 36; radius: 18
                width: chipRow.implicitWidth + 28
                color: "#15171a"; border.width: 1; border.color: Theme.outline
                Row {
                    id: chipRow
                    anchors.centerIn: parent
                    spacing: 10
                    Rectangle {
                        width: 8; height: 8; radius: 4; anchors.verticalCenter: parent.verticalCenter
                        color: vex.muted ? Theme.label : Theme.statusDot
                        SequentialAnimation on opacity {
                            running: !vex.muted && vex.state !== "idle"; loops: Animation.Infinite
                            NumberAnimation { to: 0.35; duration: 700; easing.type: Easing.InOutSine }
                            NumberAnimation { to: 1.0; duration: 700; easing.type: Easing.InOutSine }
                        }
                    }
                    SText { text: vex.headline; font.pixelSize: 14 }
                }
            }
            Rectangle {
                visible: vex.followupEnabled && vex.followupSeconds > 0
                height: 36; radius: 18; width: fu.implicitWidth + 28
                color: "#15171a"; border.width: 1; border.color: Theme.outline
                SText { id: fu; anchors.centerIn: parent; mono: true; secondary: true; font.pixelSize: 13
                        text: "follow-up " + vex.followupSeconds + " s" }
            }
        }
        MetalButton {
            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
            text: vex.muted ? "Unmute mic" : "Mute mic"
            icon: appPaths.icons + (vex.muted ? "mic_off.svg" : "mic.svg")
            checked: vex.muted
            onClicked: vex.setMuted(!vex.muted)
        }
    }

    // ---- confirm banner ---------------------------------------------------------------------------------------------------------
    Panel {
        id: banner
        anchors { left: parent.left; right: parent.right; top: header.bottom; topMargin: visible ? 14 : 0 }
        height: visible ? 64 : 0
        visible: vex.confirm !== null && vex.confirm !== undefined
        opacity: visible ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.motion; easing.type: Theme.easing } }
        padding: 14
        Row {
            anchors.verticalCenter: parent.verticalCenter
            spacing: 14
            TierPill { tier: banner.visible ? vex.confirm.tier : "yellow"; anchors.verticalCenter: parent.verticalCenter }
            SText { text: banner.visible ? "Vex is asking: " + vex.confirm.title : ""; anchors.verticalCenter: parent.verticalCenter
                    width: Math.min(implicitWidth, banner.width - 360) }
        }
        SectionLabel { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; text: "Confirm on Vex's screen" }
    }

    // ---- bottom tiles -----------------------------------------------------------------------------------------------------------
    Row {
        id: tiles
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: 128
        spacing: 20
        readonly property real w: (width - 2 * spacing) / 3
        Panel {
            width: tiles.w; height: parent.height
            Column {
                spacing: 8
                SectionLabel { text: "Response time" }
                Row {
                    spacing: 8
                    SText { mono: true; font.pixelSize: 30; font.weight: Font.Light; text: root.fmt(home.latency.median, " s") }
                    SText { mono: true; secondary: true; font.pixelSize: 13; anchors.baseline: parent.children[0].baseline; text: "median" }
                }
                SText { mono: true; secondary: true; font.pixelSize: 13
                        text: "first audio " + root.fmt(home.latency.firstAudio, " s") + " · " + (home.latency.turns || 0) + " turns" }
            }
        }
        Panel {
            width: tiles.w; height: parent.height
            Column {
                width: parent.width
                spacing: 8
                SectionLabel { text: "Health" }
                SText { width: parent.width; text: home.health.summary; font.pixelSize: 15; wrapMode: Text.WordWrap; maximumLineCount: 2 }
                SText { mono: true; secondary: true; font.pixelSize: 13; visible: home.health.ran; text: "checked " + home.health.at }
            }
            MetalButton {
                anchors.right: parent.right; anchors.bottom: parent.bottom
                text: home.healthBusy ? "Checking…" : "Run check"
                enabled: !home.healthBusy
                height: 40
                onClicked: home.runHealth()
            }
        }
        Panel {
            width: tiles.w; height: parent.height
            Column {
                width: parent.width
                spacing: 8
                SectionLabel { text: "Budget" }
                Row {
                    spacing: 6
                    SText { mono: true; font.pixelSize: 30; font.weight: Font.Light; text: "₹" + Math.round(home.budget.used || 0) }
                    SText { mono: true; secondary: true; font.pixelSize: 15; anchors.baseline: parent.children[0].baseline
                            text: "/ ₹" + Math.round(home.budget.total || 1000) }
                }
                Rectangle {
                    width: parent.width; height: 8; radius: 4; color: "#0f1113"; border.width: 1; border.color: Theme.outline
                    Rectangle {
                        width: parent.width * (home.budget.fraction || 0); height: parent.height; radius: 4
                        visible: width >= 2
                        gradient: Gradient { orientation: Gradient.Horizontal
                            GradientStop { position: 0; color: Theme.chromeBottom } GradientStop { position: 1; color: Theme.chromeTop } }
                        Behavior on width { NumberAnimation { duration: Theme.motion; easing.type: Theme.easing } }
                    }
                }
            }
        }
    }

    // ---- right column: Today + Activity -----------------------------------------------------------------------------------------
    Column {
        id: right
        visible: !root.narrow
        width: root.narrow ? 0 : Math.min(360, root.width * 0.34)
        anchors { right: parent.right; top: banner.bottom; topMargin: 20; bottom: tiles.top; bottomMargin: 20 }
        spacing: 20
        Panel {
            id: todayPanel
            width: parent.width
            height: Math.min(220, 82 + Math.max(1, home.today.length) * 34)
            Column {
                width: parent.width
                spacing: 12
                Row {
                    width: parent.width
                    SectionLabel { text: "Today"; width: parent.width - dateText.width }
                    SText { id: dateText; mono: true; secondary: true; font.pixelSize: 13; text: Qt.formatDate(new Date(), "ddd d MMM") }
                }
                SText { visible: home.today.length === 0; secondary: true; text: "Nothing due today." }
                Repeater {
                    model: home.today
                    delegate: Row {
                        required property var modelData
                        spacing: 12
                        width: todayPanel.width - 40
                        SText { mono: true; text: modelData.time; color: modelData.overdue ? Theme.secondary : Theme.text; font.pixelSize: 14 }
                        SText { text: modelData.text; width: parent.width - 70; font.pixelSize: 14 }
                    }
                }
            }
        }
        Panel {
            id: activityPanel
            width: parent.width
            height: parent.height - todayPanel.height - parent.spacing
            Item {
                anchors.fill: parent
                Row {
                    id: actHead
                    width: parent.width
                    SectionLabel { text: "Activity"; width: parent.width - auditLink.width }
                    Text {
                        id: auditLink
                        text: "Audit log ›"
                        color: linkMouse.containsMouse || activeFocus ? Theme.text : Theme.secondary
                        font.family: Theme.sans; font.pixelSize: 13; renderType: Text.NativeRendering
                        activeFocusOnTab: true
                        Keys.onReturnPressed: root.navigate("tools")
                        Accessible.role: Accessible.Link; Accessible.name: "Audit log"
                        MouseArea { id: linkMouse; anchors.fill: parent; anchors.margins: -12; hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor; onClicked: root.navigate("tools") }
                        Rectangle { anchors.fill: parent; anchors.margins: -4; radius: 4; color: "transparent"; border.width: 2
                                    border.color: Theme.focus; opacity: parent.activeFocus ? 0.75 : 0 }
                    }
                }
                SText { anchors.top: actHead.bottom; anchors.topMargin: 14; visible: home.activity.length === 0; secondary: true
                        text: "Nothing yet this session." }
                ListView {
                    anchors { top: actHead.bottom; topMargin: 12; left: parent.left; right: parent.right; bottom: parent.bottom }
                    clip: true
                    spacing: 2
                    model: home.activity
                    boundsBehavior: Flickable.StopAtBounds
                    delegate: Item {
                        required property var modelData
                        width: ListView.view.width
                        height: 44
                        Rectangle {
                            id: dot
                            width: 8; height: 8; radius: 4; y: 9
                            color: modelData.tier ? Theme.tierColor(modelData.tier) : Theme.label
                        }
                        SText { id: tm; x: 18; y: 2; mono: true; secondary: true; font.pixelSize: 12; text: modelData.time }
                        SText { x: 18 + 48; y: 1; width: parent.width - x; mono: modelData.mono; font.pixelSize: 14; text: modelData.title
                                color: modelData.ok ? Theme.text : Theme.secondary }
                        SText { x: 18 + 48; y: 22; width: parent.width - x; secondary: true; font.pixelSize: 12; text: modelData.detail }
                    }
                }
            }
        }
    }

    // ---- centre: orb, greeting, input, chips ------------------------------------------------------------------------------------
    Item {
        id: centre
        anchors { left: parent.left; right: right.left; rightMargin: root.narrow ? 0 : 20; top: banner.bottom; topMargin: 8
                  bottom: tiles.top; bottomMargin: 20 }
        Column {
            anchors.centerIn: parent
            width: Math.min(parent.width, 640)
            spacing: 16
            Orb {
                id: bigOrb
                readonly property real s: Math.max(120, Math.min(300, centre.height - 250))
                width: s; height: s
                anchors.horizontalCenter: parent.horizontalCenter
                vexState: vex.muted ? "muted" : (vex.asleep ? "asleep" : vex.state)
                Accessible.name: "Vex is " + vex.state
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: root.greeting()
                color: Theme.text; font.family: Theme.sans; font.pixelSize: 30; font.weight: Font.Light
                renderType: Text.NativeRendering
            }
            SText {
                anchors.horizontalCenter: parent.horizontalCenter
                secondary: true
                text: "Say “Hey Vex”, press " + vex.hotkey + ", or type below"
            }
            Panel {
                id: inputBar
                width: parent.width
                height: 64
                padding: 10
                radius: 18
                Row {
                    anchors.fill: parent
                    spacing: 10
                    MetalButton {
                        round: true
                        icon: appPaths.icons + "mic.svg"
                        accessibleName: "Listen"
                        onClicked: vex.listen()
                    }
                    TextField {
                        id: field
                        width: parent.width - 44 - sendButton.width - 20
                        height: 44
                        focus: true
                        placeholderText: "Ask Vex anything…"
                        placeholderTextColor: Theme.label
                        color: Theme.text
                        font.family: Theme.sans
                        font.pixelSize: 15
                        selectByMouse: true
                        verticalAlignment: TextInput.AlignVCenter
                        background: Rectangle {
                            radius: Theme.buttonRadius
                            color: "#0f1113"
                            border.width: field.activeFocus ? 2 : 1
                            border.color: field.activeFocus ? Qt.rgba(0.9, 0.91, 0.93, 0.6) : Theme.outline
                        }
                        Accessible.name: "Command"
                        onAccepted: { vex.send(text); text = "" }
                    }
                    MetalButton {
                        id: sendButton
                        chrome: true
                        text: "Send"
                        width: 96
                        enabled: field.text.trim().length > 0
                        onClicked: { vex.send(field.text); field.text = ""; field.forceActiveFocus() }
                    }
                }
            }
            Flow {
                width: parent.width
                spacing: 10
                Repeater {
                    model: [
                        { label: "Set a reminder", prefill: "Remind me to " },
                        { label: "Open an app", prefill: "Open " },
                        { label: "Search the web", prefill: "Search the web for " },
                        { label: "Remember something", prefill: "Remember that " }
                    ]
                    delegate: MetalButton {
                        required property var modelData
                        text: modelData.label
                        height: Theme.touch
                        onClicked: { field.text = modelData.prefill; field.forceActiveFocus(); field.cursorPosition = field.text.length }
                    }
                }
            }
        }
    }
}
