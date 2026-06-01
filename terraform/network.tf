resource "hcloud_network" "main" {
  name     = "k3s-network"
  ip_range = "10.0.0.0/16"

  labels = {
    project = "aramakisai"
    managed = "terraform"
  }
}

resource "hcloud_network_subnet" "nodes" {
  network_id   = hcloud_network.main.id
  type         = "cloud"
  network_zone = "eu-central" # fsn1 / nbg1 / hel1 が属するゾーン
  ip_range     = "10.0.1.0/24"
}
