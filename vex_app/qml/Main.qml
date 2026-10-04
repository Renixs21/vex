import QtQuick
import QtQuick.Window
import "components"
import "screens"

Window {
    id: win
    width: 1280
    height: 800
    minimumWidth: 900
    minimumHeight: 640
    visible: true
    color: Theme.base
    title: "Vex Control Center"

    property string current: startScreen
    readonly property bool compact: width < 1100
    readonly property var titles: ({ home: "Home", conversations: "Conversations", memory: "Memory", tasks: "Tasks & reminders",
                                     tools: "Tools & permissions", voice: "Voice", system: "System & health", budget: "Budget" })

    Sidebar {
        id: sidebar
        anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
        compact: win.compact
        current: win.current
        readonly property bool onMemory: win.current === "memory" && vex.connected
        bottomTitle: onMemory ? "Vault" : (vex.connected ? "Vex is running" : "Vex isn't running")
        bottomDetail: onMemory ? memory.size.text : (vex.connected ? "uptime " + vex.uptime : "retrying…")
        bottomFraction: onMemory ? memory.size.fraction : -1
        onNavigate: (screen) => win.current = screen
    }
    Rectangle { anchors { left: sidebar.right; top: parent.top; bottom: parent.bottom } width: 1; color: "#15171a" }

    Item {
        id: content
        anchors { left: sidebar.right; right: parent.right; top: parent.top; bottom: parent.bottom; margins: 28 }

        Loader {
            id: page
            anchors.fill: parent
            focus: true
            active: vex.connected
            sourceComponent: win.current === "home" ? homeComp : win.current === "memory" ? memoryComp
                           : win.current === "tools" ? toolsComp : placeholderComp
            opacity: 0
            onLoaded: { opacity = 0; slide.restart() }
            transform: Translate { id: shift; x: 0 }
            ParallelAnimation {
                id: slide
                NumberAnimation { target: page; property: "opacity"; from: 0; to: 1; duration: 220; easing.type: Easing.OutCubic }
                NumberAnimation { target: shift; property: "x"; from: 18; to: 0; duration: 220; easing.type: Easing.OutCubic }
            }
        }
        Offline { anchors.fill: parent; visible: !vex.connected }
    }

    Component { id: homeComp; Home { onNavigate: (screen) => win.current = screen } }
    Component { id: memoryComp; Memory {} }
    Component { id: toolsComp; Tools {} }
    Component { id: placeholderComp; Placeholder { title: win.titles[win.current] || "" } }

    // a short message at the bottom (e.g. "Starting Vex...")
    Panel {
        id: toast
        property string text: ""
        width: toastText.implicitWidth + 48; height: 48; padding: 14; radius: 14
        anchors.horizontalCenter: content.horizontalCenter
        y: win.height - (opacity > 0 ? 76 : 40)
        opacity: 0
        SText { id: toastText; anchors.centerIn: parent; text: toast.text; font.pixelSize: 14 }
        Behavior on y { NumberAnimation { duration: Theme.motion; easing.type: Theme.easing } }
        Behavior on opacity { NumberAnimation { duration: Theme.motion; easing.type: Theme.easing } }
        Timer { id: toastTimer; interval: 3200; onTriggered: toast.opacity = 0 }
    }
    Connections {
        target: vex
        function onToast(message) { toast.text = message; toast.opacity = 1; toastTimer.restart() }
    }
}
