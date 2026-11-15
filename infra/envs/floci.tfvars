# Emulador local Floci. Uso: ./scripts/tf.sh <dev|test|prod> plan|apply
target         = "floci"
floci_endpoint = "http://localhost:4566"

# En Floci los pods corren en minikube: solo se crea el plano de control de EKS.
eks_enable_node_group = false

# Floci 2.4 no implementa VPC Endpoint Services (PrivateLink). En AWS real se crea.
enable_privatelink = false
