##############################################################################
# DeepStream WebRTC — Production Makefile
# שימוש: make <target>
# דוגמה: make setup    (פעם ראשונה בלבד)
#         make deploy   (כל פעם שמשנים קוד)
##############################################################################

AWS_REGION   ?= us-east-1
CLUSTER_NAME ?= deepstream-webrtc
NAMESPACE    ?= deepstream-webrtc
GITHUB_ORG   ?= guyazulay2
GITHUB_REPO  ?= deepstream-webrtc-klv
IMAGE_TAG    ?= $(shell git rev-parse --short HEAD)

# ECR URL — נוצר ע"י Terraform, מועבר כ: make deploy ECR_URL=...
ECR_URL      ?=

.PHONY: help setup bootstrap tf-init tf-plan tf-apply \
        build-push push-secrets deploy get-url clean

help: ## הצג עזרה
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─────────────────────────────────────────────────────────────────────────────
# STEP 0: בדיקת כלים
# ─────────────────────────────────────────────────────────────────────────────
check-tools: ## בדוק שכל הכלים מותקנים
	@echo "Checking tools..."
	@command -v aws       >/dev/null || (echo "❌ aws cli missing"       && exit 1)
	@command -v terraform >/dev/null || (echo "❌ terraform missing"     && exit 1)
	@command -v kubectl   >/dev/null || (echo "❌ kubectl missing"       && exit 1)
	@command -v helm      >/dev/null || (echo "❌ helm missing"          && exit 1)
	@command -v docker    >/dev/null || (echo "❌ docker missing"        && exit 1)
	@aws sts get-caller-identity --query Account --output text >/dev/null || \
	  (echo "❌ aws configure not set" && exit 1)
	@echo "✅ All tools OK"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Bootstrap (פעם אחת בלבד — יוצר S3 + DynamoDB)
# ─────────────────────────────────────────────────────────────────────────────
bootstrap: check-tools ## [ONCE] צור S3 bucket + DynamoDB לTerraform state
	@echo "━━━ Bootstrap: Creating S3 + DynamoDB ━━━"
	cd infrastructure/terraform/bootstrap && \
	  terraform init && \
	  terraform apply -auto-approve
	@echo "✅ Bootstrap complete"
	@echo "   Copy the s3_bucket_name output and update infrastructure/terraform/backend.hcl"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Terraform — בניית תשתית AWS
# ─────────────────────────────────────────────────────────────────────────────
tf-init: ## terraform init
	cd infrastructure/terraform/environments/production && \
	  terraform init -backend-config=../../backend.hcl

tf-plan: tf-init ## terraform plan (ראה מה ישתנה לפני apply)
	cd infrastructure/terraform/environments/production && \
	  terraform plan -var="github_org=$(GITHUB_ORG)" -var="github_repo=$(GITHUB_REPO)"

tf-apply: tf-init ## [INFRA] הקם VPC + EKS + ECR + IAM
	@echo "━━━ Terraform Apply ━━━"
	cd infrastructure/terraform/environments/production && \
	  terraform apply -auto-approve \
	    -var="github_org=$(GITHUB_ORG)" \
	    -var="github_repo=$(GITHUB_REPO)"
	@echo ""
	@echo "━━━ Outputs ━━━"
	cd infrastructure/terraform/environments/production && terraform output

tf-destroy: ## ⚠️  הרוס הכל (שאל לפני!)
	@read -p "Are you sure? Type 'yes': " CONFIRM; [ "$$CONFIRM" = "yes" ] || exit 1
	cd infrastructure/terraform/environments/production && \
	  terraform destroy -var="github_org=$(GITHUB_ORG)"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: kubectl config
# ─────────────────────────────────────────────────────────────────────────────
kubeconfig: ## חבר kubectl לcluster
	aws eks update-kubeconfig --name $(CLUSTER_NAME) --region $(AWS_REGION)
	kubectl cluster-info

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Push secrets לAWS Secrets Manager
# ─────────────────────────────────────────────────────────────────────────────
push-secrets: ## דחוף secrets מ-secrets/production.env לAWS Secrets Manager
	@test -f secrets/production.env || \
	  (echo "❌ Create secrets/production.env first (copy from .example)" && exit 1)
	@echo "━━━ Pushing secrets to AWS Secrets Manager ━━━"
	@PAYLOAD=$$(cat secrets/production.env | grep -v '^#' | grep '=' | \
	  awk -F= '{printf "\"%-s\":\"%s\",", $$1, $$2}' | sed 's/,$$//' ) ; \
	  aws secretsmanager create-secret \
	    --name "deepstream-webrtc/production" \
	    --secret-string "{$${PAYLOAD}}" \
	    --region $(AWS_REGION) 2>/dev/null || \
	  aws secretsmanager update-secret \
	    --secret-id "deepstream-webrtc/production" \
	    --secret-string "{$${PAYLOAD}}" \
	    --region $(AWS_REGION)
	@echo "✅ Secrets pushed"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5: Build + Push Docker image
# ─────────────────────────────────────────────────────────────────────────────
build-push: ## בנה image ודחוף ל-ECR
	@test -n "$(ECR_URL)" || (echo "❌ ECR_URL not set. Run: make build-push ECR_URL=<url>" && exit 1)
	@echo "━━━ Build & Push: $(ECR_URL):$(IMAGE_TAG) ━━━"
	aws ecr get-login-password --region $(AWS_REGION) | \
	  docker login --username AWS --password-stdin $(ECR_URL)
	docker build -t $(ECR_URL):$(IMAGE_TAG) -t $(ECR_URL):latest .
	docker push $(ECR_URL):$(IMAGE_TAG)
	docker push $(ECR_URL):latest
	@echo "✅ Image pushed: $(ECR_URL):$(IMAGE_TAG)"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6: Deploy
# ─────────────────────────────────────────────────────────────────────────────
deploy: ## Deploy האפליקציה ל-EKS עם Helm
	@test -n "$(ECR_URL)" || (echo "❌ ECR_URL not set. Run: make deploy ECR_URL=<url>" && exit 1)
	@echo "━━━ Helm Deploy ━━━"
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	helm upgrade --install deepstream-webrtc ./helm/deepstream-webrtc \
	  --namespace $(NAMESPACE) \
	  --set image.repository=$(ECR_URL) \
	  --set image.tag=$(IMAGE_TAG) \
	  --values helm/deepstream-webrtc/values-production.yaml \
	  --atomic --wait --timeout 5m
	@echo ""
	@echo "✅ Deploy complete"
	@$(MAKE) get-url

# ─────────────────────────────────────────────────────────────────────────────
# Utils
# ─────────────────────────────────────────────────────────────────────────────
get-url: ## קבל את ה-URL הציבורי של ה-ALB
	@echo "━━━ Your public URLs ━━━"
	@echo "HTTP (ALB):"
	@kubectl get ingress -n $(NAMESPACE) \
	  -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}' 2>/dev/null && echo || \
	  echo "  (waiting for ALB... try again in 2 min)"
	@echo ""
	@echo "UDP/RTSP (NLB):"
	@kubectl get svc deepstream-webrtc-udp -n $(NAMESPACE) \
	  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null && echo || \
	  echo "  (waiting for NLB...)"

status: ## הצג סטטוס pods + services
	kubectl get pods,svc,ingress -n $(NAMESPACE)

logs: ## Stream logs מה-pods
	kubectl logs -n $(NAMESPACE) -l app=deepstream-webrtc -f --tail=100

rollback: ## Rollback לגרסה הקודמת
	helm rollback deepstream-webrtc -n $(NAMESPACE)

# ─────────────────────────────────────────────────────────────────────────────
# One-shot: הכל מאפס
# ─────────────────────────────────────────────────────────────────────────────
setup: check-tools bootstrap tf-apply kubeconfig push-secrets ## [ONCE] הכל מאפס (bootstrap → infra → kubeconfig → secrets)
	@ECR_URL=$$(cd infrastructure/terraform/environments/production && terraform output -raw ecr_url) ; \
	  $(MAKE) build-push ECR_URL=$$ECR_URL && \
	  $(MAKE) deploy ECR_URL=$$ECR_URL
