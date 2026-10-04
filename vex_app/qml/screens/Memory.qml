import QtQuick
import QtQuick.Controls.Basic
import QtWebEngine
import "../components"

// Memory: Focus (a note and its neighbours to 2 hops) | Galaxy (everything) | Timeline (what Vex learned) | Notes (cards) + search.
// Right: the selected note (Open / Edit / Forget, Forget asks first) and the inbox (Keep / Discard).
FocusScope {
    id: root
    property int tab: 0                                   // 0 Focus, 1 Galaxy, 2 Timeline, 3 Notes

    Component.onCompleted: { memory.refresh(); if (typeof startTab !== "undefined" && startTab > 0) pickTab(startTab) }
    function pickTab(i) {
        tab = i
        if (i === 1) memory.loadGalaxy()
        if (i === 2) memory.loadTimeline()
        if (i <= 1) web.push()
    }

    // ---- top bar: tabs + search ---------------------------------------------------------------------------------------------------
    SegmentedTabs {
        id: tabs
        anchors { left: parent.left; top: parent.top }
        tabs: ["Focus", "Galaxy", "Timeline", "Notes"]
        current: root.tab
        onPicked: (i) => root.pickTab(i)
    }
    TextField {
        id: search
        anchors { right: parent.right; top: parent.top }
        width: Math.min(360, root.width * 0.32)
        height: Theme.touch
        placeholderText: "Search memory…"
        placeholderTextColor: Theme.label
        color: Theme.text
        font.family: Theme.sans; font.pixelSize: 15
        leftPadding: 14
        verticalAlignment: TextInput.AlignVCenter
        background: Rectangle { radius: Theme.buttonRadius; color: "#0f1113"; border.width: search.activeFocus ? 2 : 1
                                border.color: search.activeFocus ? Qt.rgba(0.9, 0.91, 0.93, 0.6) : Theme.outline }
        Accessible.name: "Search memory"
        onTextChanged: searchDelay.restart()
        Keys.onDownPressed: if (results.count) { results.forceActiveFocus(); results.currentIndex = 0 }
        Keys.onEscapePressed: text = ""
        Timer { id: searchDelay; interval: 220; onTriggered: memory.search(search.text) }
    }

    // ---- right column: the note + inbox -----------------------------------------------------------------------------------------
    Column {
        id: right
        width: Math.min(380, root.width * 0.36)
        anchors { right: parent.right; top: tabs.bottom; topMargin: 20; bottom: parent.bottom }
        spacing: 20
        Panel {
            id: notePanel
            width: parent.width
            height: parent.height - inboxPanel.height - parent.spacing
            Flickable {
                id: noteFlick
                anchors.fill: parent
                contentHeight: noteCol.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                Column {
                    id: noteCol
                    width: noteFlick.width - 8
                    spacing: 14
                    SText { visible: !memory.note.id; secondary: true; text: "Pick a note in the graph, the timeline or the list." }
                    TypePill { visible: !!memory.note.id; label: memory.note.typeLabel || ""; dot: memory.note.color || "#b9bec6" }
                    Text {
                        visible: !!memory.note.id
                        width: parent.width
                        text: memory.note.title || ""
                        color: Theme.text; font.family: Theme.sans; font.pixelSize: 24; font.weight: Font.Normal
                        wrapMode: Text.WordWrap; renderType: Text.NativeRendering
                    }
                    SText { visible: !!memory.note.body; width: parent.width; secondary: true; wrapMode: Text.WordWrap; elide: Text.ElideNone
                            text: memory.note.body || ""; font.pixelSize: 14 }
                    Flow {
                        visible: (memory.note.links || []).length > 0
                        width: parent.width
                        spacing: 8
                        Repeater {
                            model: memory.note.links || []
                            delegate: MetalButton {
                                required property var modelData
                                text: "[[" + modelData.title + "]]"
                                mono: true
                                height: 36
                                onClicked: memory.select(modelData.id)
                            }
                        }
                    }
                    SectionLabel { visible: (memory.note.facts || []).length > 0; text: "What Vex knows" }
                    Repeater {
                        model: memory.note.facts || []
                        delegate: Column {
                            required property var modelData
                            width: noteCol.width
                            spacing: 3
                            SText { width: parent.width; text: modelData.text; wrapMode: Text.WordWrap; elide: Text.ElideNone; font.pixelSize: 14 }
                            SText { width: parent.width; mono: true; secondary: true; font.pixelSize: 12
                                    text: (modelData.learned ? "learned " + modelData.learned : "") + (modelData.source ? " · " + modelData.source : "") }
                        }
                    }
                    SectionLabel { visible: (memory.note.backlinks || []).length > 0; text: "Mentioned in" }
                    Flow {
                        width: parent.width
                        spacing: 8
                        Repeater {
                            model: memory.note.backlinks || []
                            delegate: MetalButton { required property var modelData; text: modelData.title; height: 36
                                                    onClicked: memory.select(modelData.id) }
                        }
                    }
                    Row {
                        visible: !!memory.note.id
                        spacing: 10
                        MetalButton { text: "Open"; onClicked: memory.openNote() }
                        MetalButton { text: "Edit"; onClicked: memory.editNote() }
                        MetalButton { text: "Forget…"; onClicked: memory.askForget() }
                    }
                }
            }
        }
        Panel {
            id: inboxPanel
            width: parent.width
            height: Math.min(260, 64 + Math.max(1, memory.inbox.length) * 92)
            Item {
                anchors.fill: parent
                SectionLabel { id: inboxHead; text: "Inbox" + (memory.inbox.length ? "  " + memory.inbox.length : "") }
                SText { anchors.top: inboxHead.bottom; anchors.topMargin: 12; visible: memory.inbox.length === 0; secondary: true
                        text: "Nothing waiting. Facts Vex isn't sure about land here for you to keep or discard." ; width: parent.width
                        wrapMode: Text.WordWrap; elide: Text.ElideNone; font.pixelSize: 14 }
                ListView {
                    anchors { top: inboxHead.bottom; topMargin: 12; left: parent.left; right: parent.right; bottom: parent.bottom }
                    clip: true
                    spacing: 10
                    model: memory.inbox
                    delegate: Item {
                        required property var modelData
                        required property int index
                        width: ListView.view.width
                        height: 82
                        SText { width: parent.width; text: modelData.text; font.pixelSize: 14; maximumLineCount: 1 }
                        SText { y: 20; width: parent.width; mono: true; secondary: true; font.pixelSize: 12; text: modelData.detail }
                        Row {
                            y: 38
                            spacing: 8
                            MetalButton { text: "Keep"; height: Theme.touch; onClicked: memory.keep(index) }
                            MetalButton { text: "Discard"; height: Theme.touch; onClicked: memory.discard(index) }
                        }
                    }
                }
            }
        }
    }

    // ---- the view area -----------------------------------------------------------------------------------------------------------
    Panel {
        id: viewPanel
        anchors { left: parent.left; right: right.left; rightMargin: 20; top: tabs.bottom; topMargin: 20; bottom: parent.bottom }
        padding: 0

        WebEngineView {
            id: web
            anchors.fill: parent
            anchors.margins: 1
            visible: root.tab <= 1
            backgroundColor: "transparent"
            url: appPaths.web + "graph.html"
            property bool ready: false
            settings.localContentCanAccessFileUrls: true
            settings.showScrollBars: false
            function push() {
                if (!ready) return
                if (root.tab === 0) runJavaScript("vexGraph.focus(" + JSON.stringify(memory.focusGraph) + ")")
                else if (root.tab === 1) runJavaScript("vexGraph.galaxy(" + JSON.stringify(memory.galaxy) + ")")
            }
            onTitleChanged: {
                const t = title.split("#")[0]
                if (t === "ready") { ready = true; push() }
                else if (t.indexOf("select:") === 0) { memory.select(t.slice(7)); if (root.tab !== 0) root.pickTab(0) }
            }
            onContextMenuRequested: (request) => request.accepted = true          // no browser menu
        }

        ListView {
            id: timelineView
            anchors.fill: parent
            anchors.margins: 22
            visible: root.tab === 2
            clip: true
            spacing: 22
            model: memory.timeline
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            delegate: Column {
                required property var modelData
                width: timelineView.width - 12
                spacing: 10
                SText { mono: true; secondary: true; font.pixelSize: 13; text: modelData.label }
                Repeater {
                    model: modelData.items
                    delegate: Item {
                        required property var modelData
                        width: timelineView.width - 12
                        height: 50
                        Rectangle { width: 8; height: 8; radius: 4; y: 7; color: modelData.color }
                        SText { x: 20; width: parent.width - 20; text: modelData.text; font.pixelSize: 14 }
                        Row {
                            x: 20; y: 24; spacing: 10
                            Text {
                                text: modelData.title; color: Theme.secondary; font.family: Theme.sans; font.pixelSize: 13
                                font.underline: tlHover.containsMouse; renderType: Text.NativeRendering
                                MouseArea { id: tlHover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                            onClicked: memory.select(modelData.note_id) }
                            }
                            SText { mono: true; secondary: true; font.pixelSize: 12; text: modelData.source }
                        }
                    }
                }
            }
            SText { visible: timelineView.count === 0; anchors.centerIn: parent; secondary: true; text: "Nothing learned yet." }
        }

        GridView {
            id: notesView
            anchors.fill: parent
            anchors.margins: 18
            visible: root.tab === 3
            clip: true
            readonly property var filtered: memory.notes.filter(n => !search.text.trim()
                                                                || n.title.toLowerCase().indexOf(search.text.trim().toLowerCase()) >= 0)
            model: filtered
            cellWidth: Math.max(220, width / Math.max(1, Math.floor(width / 240)))
            cellHeight: 128
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            delegate: Item {
                required property var modelData
                width: notesView.cellWidth
                height: notesView.cellHeight
                Panel {
                    anchors.fill: parent
                    anchors.margins: 6
                    padding: 16
                    radius: 14
                    activeFocusOnTab: true
                    Keys.onReturnPressed: { memory.select(modelData.id); root.pickTab(0) }
                    Column {
                        width: parent.width
                        spacing: 8
                        TypePill { label: modelData.typeLabel; dot: modelData.color }
                        SText { width: parent.width; text: modelData.title; font.pixelSize: 16 }
                        SText { mono: true; secondary: true; font.pixelSize: 12
                                text: modelData.facts + (modelData.facts === 1 ? " fact" : " facts") + " · " + modelData.updatedText }
                    }
                    MouseArea { anchors.fill: parent; anchors.margins: -16; cursorShape: Qt.PointingHandCursor
                                onClicked: memory.select(modelData.id) }
                }
            }
        }
    }

    // ---- search results (drop-down) ------------------------------------------------------------------------------------------------
    Panel {
        id: resultsPanel
        visible: search.text.trim().length > 0 && memory.results.length > 0 && root.tab !== 3
        anchors { right: parent.right; top: search.bottom; topMargin: 6 }
        width: search.width
        height: Math.min(360, 20 + memory.results.length * 56)
        padding: 10
        z: 20
        ListView {
            id: results
            anchors.fill: parent
            clip: true
            model: memory.results
            keyNavigationEnabled: true
            highlightFollowsCurrentItem: true
            Keys.onReturnPressed: if (currentIndex >= 0) { memory.select(memory.results[currentIndex].id); search.text = ""; root.pickTab(0) }
            Keys.onEscapePressed: { search.text = ""; search.forceActiveFocus() }
            highlight: Rectangle { radius: 8; color: "#1d2024"; border.width: 1; border.color: Theme.outline }
            delegate: Item {
                required property var modelData
                required property int index
                width: ListView.view.width
                height: 54
                Rectangle { x: 10; y: 12; width: 8; height: 8; radius: 4; color: modelData.color }
                SText { x: 28; y: 6; width: parent.width - 36; text: modelData.title; font.pixelSize: 14 }
                SText { x: 28; y: 27; width: parent.width - 36; secondary: true; font.pixelSize: 12; text: modelData.snippet || modelData.type }
                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                            onClicked: { memory.select(modelData.id); search.text = ""; root.pickTab(0) } }
            }
        }
    }

    Connections {
        target: memory
        function onFocusGraphChanged() { if (root.tab === 0) web.push() }
        function onGalaxyChanged() { if (root.tab === 1) web.push() }
        function onRawLoaded(text) { editor.text = text; editDialog.open(); editor.forceActiveFocus() }
        function onForgetPreview(message) { forgetText.text = message; forgetDialog.open() }
    }

    // ---- dialogs -------------------------------------------------------------------------------------------------------------------
    Dialog {
        id: editDialog
        title: "Edit " + (memory.note.title || "")
        sheetWidth: 760; sheetHeight: 560
        ScrollView {
            anchors { left: parent.left; right: parent.right; top: parent.top; bottom: editButtons.top; bottomMargin: 16 }
            TextArea {
                id: editor
                color: Theme.text; font.family: Theme.mono; font.pixelSize: 13
                wrapMode: TextEdit.Wrap; selectByMouse: true
                background: Rectangle { radius: Theme.buttonRadius; color: "#0f1113"; border.width: 1; border.color: Theme.outline }
                Accessible.name: "Note text"
            }
        }
        Row {
            id: editButtons
            anchors { right: parent.right; bottom: parent.bottom }
            spacing: 10
            MetalButton { text: "Cancel"; onClicked: editDialog.close() }
            MetalButton { chrome: true; text: "Save"; width: 110; onClicked: { memory.saveNote(editor.text); editDialog.close() } }
        }
    }
    Dialog {
        id: forgetDialog
        title: "Forget " + (memory.note.title || "") + "?"
        sheetWidth: 560; sheetHeight: 340
        SText { id: forgetText; width: parent.width; wrapMode: Text.WordWrap; elide: Text.ElideNone; secondary: true; font.pixelSize: 14 }
        SText { anchors.top: forgetText.bottom; anchors.topMargin: 12; width: parent.width; wrapMode: Text.WordWrap; font.pixelSize: 13
                secondary: true; text: "The note goes to the Recycle Bin, so you can still get it back from there." }
        Row {
            anchors { right: parent.right; bottom: parent.bottom }
            spacing: 10
            MetalButton { id: keepButton; text: "Keep it"; onClicked: forgetDialog.close() }
            MetalButton { chrome: true; text: "Forget"; width: 110; onClicked: { memory.confirmForget(); forgetDialog.close() } }
        }
        onVisibleChanged: if (visible) keepButton.forceActiveFocus()      // the safe choice has the focus
    }
}
