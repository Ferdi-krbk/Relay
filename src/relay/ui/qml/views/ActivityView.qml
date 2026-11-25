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
        anchors.topMargin: 16
        anchors.bottomMargin: 20
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            MonoText {
                Layout.fillWidth: true
                elide: Text.ElideRight
                text: "Direkt: " + network.directCount + "  ·  TCP: " + network.tcpFragCount + "  ·  TLS: " + network.tlsRecordCount + "  ·  Tünel: " + network.tunnelCount + "  ·  Hata: " + network.failedCount
                color: root.theme.inkSoft
            }
            Rectangle {
                width: 106; height: 28; radius: 7; antialiasing: true
                color: logHit.containsMouse ? root.theme.recess : root.theme.card
                border.color: root.theme.hairline
                BodyText { anchors.centerIn: parent; text: "Log Klasörü ↗"; font.pixelSize: 11; font.weight: Font.Medium; color: root.theme.ink }
                PressArea { id: logHit; anchors.fill: parent; onInvoke: network.openDataFolder() }
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
                visible: network.recentEvents.length === 0
                text: network.connected
                    ? "HTTPS trafiği geldikçe burada canlı listelenecek."
                    : "Canlı akışı görmek için önce Relay'i başlat."
                color: root.theme.inkMuted
                font.pixelSize: 12
            }

            ListView {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                anchors.topMargin: 8
                anchors.bottomMargin: 8
                clip: true
                model: network.recentEvents
                visible: network.recentEvents.length > 0

                delegate: Item {
                    required property var modelData
                    width: ListView.view.width
                    height: 34

                    RowLayout {
                        anchors.fill: parent
                        spacing: 10
                        MonoText { text: modelData.time || ""; font.pixelSize: 10; color: root.theme.inkMuted; Layout.preferredWidth: 56 }
                        BodyText { Layout.fillWidth: true; text: modelData.host || ""; font.pixelSize: 12; color: root.theme.ink; elide: Text.ElideRight }
                        Rectangle {
                            width: 62; height: 20; radius: 5; antialiasing: true
                            color: modelData.strategy === "failed" ? root.theme.vermilionSoft
                                : (modelData.strategy === "direct" ? root.theme.recess : root.theme.forestSoft)
                            MonoText {
                                anchors.centerIn: parent
                                text: modelData.strategy === "tls_record" ? "TLS"
                                    : modelData.strategy === "tcp_frag" ? "TCP"
                                    : modelData.strategy === "tunnel" ? "TÜNEL"
                                    : modelData.strategy === "failed" ? "HATA"
                                    : "DİREKT"
                                font.pixelSize: 9
                                font.weight: Font.DemiBold
                                color: modelData.strategy === "failed" ? root.theme.vermilion
                                    : (modelData.strategy === "direct" ? root.theme.inkSoft : root.theme.forest)
                            }
                        }
                        MonoText {
                            Layout.preferredWidth: 52
                            horizontalAlignment: Text.AlignRight
                            text: (modelData.latency_ms || 0) + " ms"
                            font.pixelSize: 10
                            color: root.theme.inkSoft
                        }
                    }

                    Rectangle {
                        anchors.bottom: parent.bottom
                        width: parent.width; height: 1
                        color: root.theme.hairline
                    }
                }
            }
        }
    }
}
