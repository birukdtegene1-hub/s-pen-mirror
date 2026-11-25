import QtQuick
import Quickshell.Io

Item {
    id: root
    visible: false

    required property string controller
    property string receiverState: "off"
    property string statusTooltip: "S-Pen Mirror · Checking status…"
    property bool toggleFailed: false
    readonly property bool pending: action.running || receiverState === "starting" || receiverState === "stopping"
    readonly property string tooltip: action.running ? "S-Pen Mirror · Switching…"
        : toggleFailed ? "S-Pen Mirror · Could not switch. Click to retry."
        : statusTooltip

    function toggle() {
        if (!action.running) {
            toggleFailed = false
            action.running = true
        }
    }

    Process {
        id: statusCheck
        command: [root.controller, "status"]
        stdout: SplitParser {
            onRead: function(line) {
                const status = JSON.parse(line)
                if (status.class !== root.receiverState) root.toggleFailed = false
                root.receiverState = status.class
                root.statusTooltip = status.tooltip
            }
        }
        onExited: function(code) {
            if (code !== 0) {
                root.receiverState = "unavailable"
                root.statusTooltip = "S-Pen Mirror · Desktop session unavailable"
            }
        }
    }

    Process {
        id: action
        command: [root.controller, "toggle"]
        onExited: function(code) {
            root.toggleFailed = code !== 0
            statusCheck.running = true
        }
    }

    Timer {
        interval: 2000
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: statusCheck.running = true
    }
}
