# AWS real (mismo código, solo cambia este archivo y el backend).
# Uso: TF_TARGET=aws ./scripts/tf.sh <dev|test|prod> plan
target                     = "aws"
aws_region                 = "us-east-1"
eks_enable_node_group      = true
eks_endpoint_public_access = false
