import QtQuick
import QtQuick.Layouts
import "../components"

Item {
    id: root
    required property var theme

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: 26
        anchors.rightMargin: 26
        anchors.topMargin: 18
        anchors.bottomMargin: 20
        spacing: 14

        RowLayout {
            Layout.fillWidth: true
            Column {
                Layout.fillWidth: true
                spacing: 2
                DisplayText { text: "Erişim & Gecikme Testi"; font.pixelSize: 15; font.weight: Font.Medium; color: root.theme.ink }
                BodyText {
                    text: network.connected
                        ? "Tüm hedefler aktif Relay (" + network.proxyEndpoint + ") üzerinden ölçülür."
                        : "Relay kapalı — testler doğrudan mevcut bağlantınla çalışır."
                    font.pixelSize: 12
                    color: root.theme.inkSoft
                }
            }
            Row {
                spacing: 8
                Rectangle {
                    visible: network.connected
                    width: 118; height: 34; radius: 9; antialiasing: true
                    color: flushBtn.containsMouse ? root.theme.recess : root.theme.card
                    border.color: root.theme.outerBorder
                    scale: flushBtn.pressed ? 0.96 : 1.0
                    Behavior on scale { NumberAnimation { duration: 90 } }
                    BodyText {
                        anchors.centerIn: parent
                        text: "Önbelleği Temizle"
                        font.pixelSize: 11
                        font.weight: Font.Medium
                        color: root.theme.ink
                    }
                    PressArea {
                        id: flushBtn
                        anchors.fill: parent
                        enabled: network.connected && !network.busy
                        onInvoke: network.flushCaches()
                    }
                }
                Rectangle {
                    width: 124; height: 34; radius: 9; antialiasing: true
                    color: network.testing ? root.theme.recess : (tRun.containsMouse ? "#2B2722" : root.theme.ink)
                    scale: tRun.pressed ? 0.96 : 1.0
                    Behavior on scale { NumberAnimation { duration: 90 } }
                    BodyText {
                        anchors.centerIn: parent
                        text: network.testing ? "Ölçülüyor…" : "Testi Başlat"
                        font.pixelSize: 12
                        font.weight: Font.Medium
                        color: network.testing ? root.theme.inkMuted : "#FDFCFA"
                    }
                    PressArea {
                        id: tRun
                        anchors.fill: parent
                        enabled: !network.testing
                        onInvoke: network.runDiagnostics()
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 16
            antialiasing: true
            color: root.theme.card
            border.color: root.theme.hairline
            clip: true

            BodyText {
                anchors.centerIn: parent
                visible: network.diagnosticResults.length === 0 && !network.testing
                text: "Discord, Roblox, Steam, Epic Games ve Cloudflare DoH\nerişimini doğrulamak için Testi Başlat'a tıkla."
                horizontalAlignment: Text.AlignHCenter
                color: root.theme.inkMuted
                font.pixelSize: 12
            }
            BodyText {
                anchors.centerIn: parent
                visible: network.testing
                text: "Hedef sunucular paralel olarak sorgulanıyor…"
                color: root.theme.inkSoft
                font.pixelSize: 12
            }

            Column {
                anchors.fill: parent
                anchors.leftMargin: 18
                anchors.rightMargin: 18
                anchors.topMargin: 6
                anchors.bottomMargin: 6
                visible: !network.testing && network.diagnosticResults.length > 0

                Repeater {
                    model: network.diagnosticResults
                    delegate: Item {
                        required property var modelData
                        required property int index
                        width: parent.width
                        height: 51

                        RowLayout {
                            anchors.fill: parent
                            spacing: 12
                            Rectangle {
                                width: 8; height: 8; radius: 4; antialiasing: true
                                color: modelData.ok ? root.theme.forest : root.theme.vermilion
                            }
                            Column {
                                Layout.fillWidth: true
                                spacing: 1
                                BodyText { width: parent.width; elide: Text.ElideRight; text: modelData.name; font.weight: Font.Medium; color: root.theme.ink }
                                MonoText {
                                    width: parent.width
                                    text: modelData.ok ? modelData.host : (modelData.host + " · " + modelData.error)
                                    font.pixelSize: 10
                                    color: modelData.ok ? root.theme.inkMuted : root.theme.vermilion
                                    elide: Text.ElideRight
                                }
                            }
                            Rectangle {
                                width: 66; height: 22; radius: 6; antialiasing: true
                                color: modelData.ok ? root.theme.forestSoft : root.theme.vermilionSoft
                                MonoText {
                                    anchors.centerIn: parent
                                    text: modelData.status > 0 ? ("HTTP " + modelData.status) : "HATA"
                                    font.pixelSize: 10
                                    font.weight: Font.DemiBold
                                    color: modelData.ok ? root.theme.forest : root.theme.vermilion
                                }
                            }
                            MonoText {
                                Layout.preferredWidth: 64
                                horizontalAlignment: Text.AlignRight
                                text: modelData.latency_ms + " ms"
                                font.pixelSize: 12
                                font.weight: Font.Medium
                                color: root.theme.ink
                            }
                        }

                        Rectangle {
                            visible: index < network.diagnosticResults.length - 1
                            anchors.bottom: parent.bottom
                            width: parent.width; height: 1
                            color: root.theme.hairline
                        }
                    }
                }
            }
        }
    }
}
