resource "hcloud_placement_group" "k3s_nodes" {
  name = "k3s-nodes"
  type = "spread"

  labels = {
    project = "aramakisai"
    managed = "terraform"
  }
}
