##############################################################################
# deepstream-webrtc — App Makefile
# שימוש: make <target>
##############################################################################

AWS_REGION ?= us-east-1
NAMESPACE  ?= deepstream-webrtc
IMAGE_TAG  ?= $(shell git rev-parse --short HEAD)
ECR_URL    ?=

.PHONY: help check-tools dev build-push deploy get-url status logs rollback

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

check-tools: ## בדוק שכל הכלים מותקנים
	@command -v docker  >/dev/null || (echo "docker missing"  && exit 1)
	@command -v kubectl >/dev/null || (echo "kubectl missing" && exit 1)
	@command -v helm    >/dev/null || (echo "helm missing"    && exit 1)
	@echo "OK"

dev: ## הרץ locally עם docker-compose
	docker compose up --build

build-push: ## בנה image ודחוף ל-ECR  (ECR_URL=<url> חובה)
	@test -n "$(ECR_URL)" || (echo "Usage: make build-push ECR_URL=<ecr-url>" && exit 1)
	aws ecr get-login-password --region $(AWS_REGION) | \
	  docker login --username AWS --password-stdin $(ECR_URL)
	docker build -t $(ECR_URL):$(IMAGE_TAG) -t $(ECR_URL):latest .
	docker push $(ECR_URL):$(IMAGE_TAG)
	docker push $(ECR_URL):latest
	@echo "Pushed: $(ECR_URL):$(IMAGE_TAG)"

deploy: ## Deploy ל-EKS עם Helm  (ECR_URL=<url> חובה — בד"כ ArgoCD עושה זאת)
	@test -n "$(ECR_URL)" || (echo "Usage: make deploy ECR_URL=<ecr-url>" && exit 1)
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	helm upgrade --install deepstream-webrtc ./helm/deepstream-webrtc \
	  --namespace $(NAMESPACE) \
	  --set image.repository=$(ECR_URL) \
	  --set image.tag=$(IMAGE_TAG) \
	  --values helm/deepstream-webrtc/values-production.yaml \
	  --atomic --wait --timeout 5m
	@$(MAKE) get-url

get-url: ## הצג URLs ציבוריים (ALB + NLB)
	@echo "=== UI (ALB) ==="
	@kubectl get ingress -n $(NAMESPACE) \
	  -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}' 2>/dev/null \
	  && echo || echo "  (not ready yet — wait 2min)"
	@echo "=== UDP Stream (NLB) ==="
	@kubectl get svc deepstream-webrtc-udp -n $(NAMESPACE) \
	  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null \
	  && echo || echo "  (not ready yet)"

status: ## סטטוס pods + services + ingress
	kubectl get pods,svc,ingress -n $(NAMESPACE)

logs: ## Stream logs
	kubectl logs -n $(NAMESPACE) -l app=deepstream-webrtc -f --tail=100

rollback: ## Rollback לגרסה הקודמת
	helm rollback deepstream-webrtc -n $(NAMESPACE)
