import QtQuick

Item {
    id: root
    property bool checked: false
    property bool switchEnabled: true
    property color activeColor: "#E54826"
    property color inactiveColor: "#E8E2D6"
    property color borderColor: "#CFC6B6"
    signal toggled(bool value)

    width: 40
    height: 22
    opacity: root.switchEnabled ? 1.0 : 0.55

    Rectangle {
        anchors.fill: parent
        radius: 11
        antialiasing: true
        color: root.checked ? root.activeColor : root.inactiveColor
        border.color: root.checked ? root.activeColor : root.borderColor
        border.width: 1
        Behavior on color { ColorAnimation { duration: 180; easing.type: Easing.OutCubic } }

        Rectangle {
            x: root.checked ? 20 : 2
            y: 2
            width: 18
            height: 18
            radius: 9
            antialiasing: true
            color: "#FFFFFF"
            border.color: "#14000000"
            Behavior on x { NumberAnimation { duration: 220; easing.type: Easing.OutExpo } }
        }
    }

    PressArea {
        anchors.fill: parent
        enabled: root.switchEnabled
        onInvoke: root.toggled(!root.checked)
    }
}
