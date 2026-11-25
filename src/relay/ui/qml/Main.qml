import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Layouts
import "components"
import "views"

ApplicationWindow {
    id: window
    width: 668
    height: 444
    minimumWidth: 668
    maximumWidth: 668
    minimumHeight: 444
    maximumHeight: 444
    visible: true
    title: "Relay"
    color: "transparent"
    flags: Qt.Window | Qt.FramelessWindowHint
    font.family: "Segoe UI Variable Text"

    Theme { id: theme }

    property int currentTab: 0
    property bool animations: network.animations
    property bool motion: animations && visible && visibility !== Window.Minimized
    property bool isHiding: false
    property real spinAngle: 0
    property real connectRipple: 0
    property bool _wasConnected: false

    property bool _introDone: !(typeof showIntro !== "undefined" ? showIntro : true)

    function playOpen() {
        isHiding = false
        hideAnim.stop()
        if (!animations) {
            introOverlay.visible = false
            shell.opacity = 1
            shellScale.xScale = 1; shellScale.yScale = 1
            shellShift.y = 0
            headerBar.opacity = 1; headerShift.y = 0
            overviewView.leftColumn.opacity = 1; overviewView.leftShift.y = 0
            overviewView.rightColumn.opacity = 1; overviewView.rightShift.y = 0
            overviewView.rightScale.xScale = 1; overviewView.rightScale.yScale = 1
            return
        }
        if (!_introDone) {
            _introDone = true
            openAnim.restart()
        } else {
            introOverlay.visible = false
            headerBar.opacity = 1; headerShift.y = 0
            overviewView.leftColumn.opacity = 1; overviewView.leftShift.y = 0
            overviewView.rightColumn.opacity = 1; overviewView.rightShift.y = 0
            overviewView.rightScale.xScale = 1; overviewView.rightScale.yScale = 1
            quickOpenAnim.restart()
        }
    }

    function requestHide() {
        if (isHiding) return
        if (!animations) {
            desktop.hideWindow()
            return
        }
        isHiding = true
        openAnim.stop()
        hideAnim.restart()
    }

    Component.onCompleted: {
        _wasConnected = network.connected
        playOpen()
    }

    onClosing: function(event) {
        event.accepted = false
        requestHide()
    }

    onCurrentTabChanged: {
        if (animations) tabReveal.restart()
    }

    Connections {
        target: network
        function onChanged() {
            if (network.connected && !window._wasConnected && window.motion) {
                connectPulseAnim.restart()
            }
            window._wasConnected = network.connected
        }
    }

    // --- Cinema-Grade Opening Choreography ---
    ParallelAnimation {
        id: openAnim
        NumberAnimation { target: shell; property: "opacity"; from: 0; to: 1; duration: 320; easing.type: Easing.OutCubic }
        NumberAnimation { target: shellScale; property: "xScale"; from: 0.92; to: 1.0; duration: 480; easing.type: Easing.OutExpo }
        NumberAnimation { target: shellScale; property: "yScale"; from: 0.92; to: 1.0; duration: 480; easing.type: Easing.OutExpo }
        NumberAnimation { target: shellShift; property: "y"; from: 26; to: 0; duration: 480; easing.type: Easing.OutExpo }

        SequentialAnimation {
            PropertyAction { target: introOverlay; property: "visible"; value: true }
            PropertyAction { target: introOverlay; property: "opacity"; value: 1 }
            PropertyAction { target: introLogo; property: "opacity"; value: 0 }
            PropertyAction { target: introLogoScale; property: "xScale"; value: 0.72 }
            PropertyAction { target: introLogoScale; property: "yScale"; value: 0.72 }
            PropertyAction { target: introWord; property: "opacity"; value: 0 }
            PropertyAction { target: introWordShift; property: "y"; value: 12 }
            PropertyAction { target: introRing; property: "opacity"; value: 0.65 }
            PropertyAction { target: introRing; property: "scale"; value: 0.4 }

            ParallelAnimation {
                NumberAnimation { target: introLogo; property: "opacity"; to: 1; duration: 280; easing.type: Easing.OutCubic }
                NumberAnimation { target: introLogoScale; property: "xScale"; to: 1.0; duration: 440; easing.type: Easing.OutExpo }
                NumberAnimation { target: introLogoScale; property: "yScale"; to: 1.0; duration: 440; easing.type: Easing.OutExpo }
                NumberAnimation { target: introRing; property: "scale"; to: 2.3; duration: 540; easing.type: Easing.OutCubic }
                NumberAnimation { target: introRing; property: "opacity"; to: 0; duration: 540; easing.type: Easing.OutCubic }
                SequentialAnimation {
                    PauseAnimation { duration: 110 }
                    ParallelAnimation {
                        NumberAnimation { target: introWord; property: "opacity"; to: 1; duration: 260; easing.type: Easing.OutCubic }
                        NumberAnimation { target: introWordShift; property: "y"; to: 0; duration: 340; easing.type: Easing.OutExpo }
                    }
                }
            }
            PauseAnimation { duration: 120 }
            ParallelAnimation {
                NumberAnimation { target: introOverlay; property: "opacity"; to: 0; duration: 260; easing.type: Easing.InOutCubic }
                NumberAnimation { target: introLogoScale; property: "xScale"; to: 1.08; duration: 260; easing.type: Easing.InCubic }
                NumberAnimation { target: introLogoScale; property: "yScale"; to: 1.08; duration: 260; easing.type: Easing.InCubic }
            }
            PropertyAction { target: introOverlay; property: "visible"; value: false }
        }

        SequentialAnimation {
            PropertyAction { target: headerBar; property: "opacity"; value: 0 }
            PropertyAction { target: headerShift; property: "y"; value: -10 }
            PauseAnimation { duration: 560 }
            ParallelAnimation {
                NumberAnimation { target: headerBar; property: "opacity"; to: 1; duration: 280; easing.type: Easing.OutCubic }
                NumberAnimation { target: headerShift; property: "y"; to: 0; duration: 360; easing.type: Easing.OutExpo }
            }
        }

        SequentialAnimation {
            PropertyAction { target: overviewView.leftColumn; property: "opacity"; value: 0 }
            PropertyAction { target: overviewView.leftShift; property: "y"; value: 20 }
            PauseAnimation { duration: 610 }
            ParallelAnimation {
                NumberAnimation { target: overviewView.leftColumn; property: "opacity"; to: 1; duration: 320; easing.type: Easing.OutCubic }
                NumberAnimation { target: overviewView.leftShift; property: "y"; to: 0; duration: 420; easing.type: Easing.OutExpo }
            }
        }

        SequentialAnimation {
            PropertyAction { target: overviewView.rightColumn; property: "opacity"; value: 0 }
            PropertyAction { target: overviewView.rightShift; property: "y"; value: 24 }
            PropertyAction { target: overviewView.rightScale; property: "xScale"; value: 0.95 }
            PropertyAction { target: overviewView.rightScale; property: "yScale"; value: 0.95 }
            PauseAnimation { duration: 660 }
            ParallelAnimation {
                NumberAnimation { target: overviewView.rightColumn; property: "opacity"; to: 1; duration: 340; easing.type: Easing.OutCubic }
                NumberAnimation { target: overviewView.rightShift; property: "y"; to: 0; duration: 460; easing.type: Easing.OutExpo }
                NumberAnimation { target: overviewView.rightScale; property: "xScale"; to: 1.0; duration: 460; easing.type: Easing.OutExpo }
                NumberAnimation { target: overviewView.rightScale; property: "yScale"; to: 1.0; duration: 460; easing.type: Easing.OutExpo }
            }
        }
    }

    ParallelAnimation {
        id: quickOpenAnim
        NumberAnimation { target: shell; property: "opacity"; from: 0; to: 1; duration: 170; easing.type: Easing.OutCubic }
        NumberAnimation { target: shellScale; property: "xScale"; from: 0.96; to: 1.0; duration: 210; easing.type: Easing.OutExpo }
        NumberAnimation { target: shellScale; property: "yScale"; from: 0.96; to: 1.0; duration: 210; easing.type: Easing.OutExpo }
        NumberAnimation { target: shellShift; property: "y"; from: 12; to: 0; duration: 210; easing.type: Easing.OutExpo }
    }

    ParallelAnimation {
        id: hideAnim
        NumberAnimation { target: shell; property: "opacity"; to: 0; duration: 230; easing.type: Easing.InCubic }
        NumberAnimation { target: shellScale; property: "xScale"; to: 0.91; duration: 250; easing.type: Easing.InCubic }
        NumberAnimation { target: shellScale; property: "yScale"; to: 0.91; duration: 250; easing.type: Easing.InCubic }
        NumberAnimation { target: shellShift; property: "y"; to: 28; duration: 250; easing.type: Easing.InCubic }
        onFinished: {
            window.isHiding = false
            desktop.hideWindow()
        }
    }

    ParallelAnimation {
        id: tabReveal
        NumberAnimation { target: tabStack; property: "opacity"; from: 0.25; to: 1.0; duration: 220; easing.type: Easing.OutCubic }
        NumberAnimation { target: tabShift; property: "y"; from: 10; to: 0; duration: 260; easing.type: Easing.OutExpo }
    }

    NumberAnimation {
        id: connectPulseAnim
        target: window; property: "connectRipple"
        from: 0; to: 1; duration: 680; easing.type: Easing.OutCubic
    }

    NumberAnimation on spinAngle {
        from: 0; to: 360
        duration: 900
        loops: Animation.Infinite
        running: window.motion && network.busy
    }

    Rectangle {
        id: shell
        anchors.fill: parent
        radius: 20
        antialiasing: true
        color: theme.canvas
        border.color: theme.outerBorder
        border.width: 1
        clip: true
        opacity: 0

        transform: [
            Scale { id: shellScale; origin.x: shell.width / 2; origin.y: shell.height / 2; xScale: 1; yScale: 1 },
            Translate { id: shellShift; y: 0 }
        ]

        Rectangle {
            x: 20; y: 1
            width: parent.width - 40; height: 1
            color: "#FFFFFF"; opacity: 0.75
        }

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            // HEADER BAR
            Item {
                id: headerBar
                Layout.fillWidth: true
                Layout.preferredHeight: 60
                transform: Translate { id: headerShift; y: 0 }

                MouseArea {
                    anchors.fill: parent
                    onPressed: window.startSystemMove()
                }

                Row {
                    x: 24
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 10

                    Image {
                        width: 28; height: 28
                        sourceSize.width: 112; sourceSize.height: 112
                        source: iconSource
                        smooth: true; mipmap: true
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    DisplayText {
                        text: "Relay"
                        font.pixelSize: 16
                        font.weight: Font.Medium
                        font.letterSpacing: -0.3
                        color: theme.ink
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }

                // Segmented Navigation
                Rectangle {
                    anchors.centerIn: parent
                    width: 284; height: 34; radius: 10; antialiasing: true
                    color: theme.recess
                    border.color: "#DDD6C8"
                    border.width: 1

                    Rectangle {
                        x: 3 + window.currentTab * 69.5
                        y: 3
                        width: 69; height: 28; radius: 7.5; antialiasing: true
                        color: theme.card
                        border.color: "#E4DDD1"
                        border.width: 1
                        Behavior on x { NumberAnimation { duration: 280; easing.type: Easing.OutExpo } }
                    }

                    Row {
                        anchors.fill: parent
                        anchors.margins: 3
                        Repeater {
                            model: ["Bağlantı", "Test", "Kurallar", "Akış"]
                            delegate: Item {
                                required property int index
                                required property string modelData
                                width: 69.5; height: 28
                                BodyText {
                                    anchors.centerIn: parent
                                    text: modelData
                                    font.pixelSize: 12
                                    font.weight: window.currentTab === index ? Font.Medium : Font.Normal
                                    color: window.currentTab === index ? theme.ink : theme.inkSoft
                                    Behavior on color { ColorAnimation { duration: 160 } }
                                }
                                PressArea {
                                    anchors.fill: parent
                                    onInvoke: window.currentTab = index
                                }
                            }
                        }
                    }
                }

                // Window Controls
                Row {
                    anchors.right: parent.right
                    anchors.rightMargin: 16
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 4

                    Repeater {
                        model: ["Küçült", "Tepsiye gizle"]
                        delegate: Rectangle {
                            required property int index
                            required property string modelData
                            width: 28; height: 28; radius: 8; antialiasing: true
                            color: winBtn.containsMouse ? theme.recess : "transparent"
                            scale: winBtn.pressed ? 0.92 : 1.0
                            Behavior on color { ColorAnimation { duration: 120 } }
                            Behavior on scale { NumberAnimation { duration: 90 } }
                            BodyText {
                                anchors.centerIn: parent
                                text: index === 0 ? "−" : "×"
                                font.pixelSize: 15
                                color: winBtn.containsMouse ? theme.ink : theme.inkMuted
                            }
                            PressArea {
                                id: winBtn
                                anchors.fill: parent
                                Accessible.name: modelData
                                onInvoke: if (index === 0) window.showMinimized(); else window.requestHide()
                            }
                        }
                    }
                }

                Rectangle {
                    anchors.bottom: parent.bottom
                    anchors.left: parent.left; anchors.right: parent.right
                    anchors.leftMargin: 24; anchors.rightMargin: 24
                    height: 1; color: theme.hairline
                }
            }

            // MODULAR STACKED VIEWS
            StackLayout {
                id: tabStack
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: window.currentTab
                clip: true
                transform: Translate { id: tabShift; y: 0 }

                OverviewView {
                    id: overviewView
                    theme: theme
                    motion: window.motion
                    spinAngle: window.spinAngle
                    connectRipple: window.connectRipple
                }
                DiagnosticsView { theme: theme }
                RulesView { theme: theme }
                ActivityView { theme: theme }
            }
        }

        // CINEMATIC BRAND REVEAL INTRO OVERLAY
        Rectangle {
            id: introOverlay
            anchors.fill: parent
            radius: 20
            color: theme.canvas
            visible: false
            z: 99

            Rectangle {
                id: introRing
                anchors.centerIn: parent
                anchors.verticalCenterOffset: -16
                width: 78; height: 78; radius: 39; antialiasing: true
                color: "transparent"
                border.color: theme.vermilion
                border.width: 2
            }

            Column {
                anchors.centerIn: parent
                spacing: 14

                Image {
                    id: introLogo
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: 62; height: 62
                    sourceSize.width: 248; sourceSize.height: 248
                    source: iconSource
                    smooth: true; mipmap: true
                    transform: Scale { id: introLogoScale; origin.x: 31; origin.y: 31; xScale: 1; yScale: 1 }
                }

                DisplayText {
                    id: introWord
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Relay"
                    font.pixelSize: 22
                    font.weight: Font.Medium
                    font.letterSpacing: -0.4
                    color: theme.ink
                    transform: Translate { id: introWordShift; y: 0 }
                }
            }
        }
    }
}
