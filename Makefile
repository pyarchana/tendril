.PHONY: up https down logs demo note seed test

up:  ## build and start app + ollama (pulls the model on first run)
	docker compose up -d --build

https:  ## same, plus Caddy for HTTPS on $DOMAIN
	docker compose --profile https up -d --build

down:
	docker compose --profile https down

logs:
	docker compose logs -f app ollama-pull

demo:  ## seed two plants, plan the week with the local model, render today's image
	docker compose exec app python -m scripts.demo

note:  ## typed check-in, e.g. make note TEXT="Watered the chillies"
	docker compose exec app python -m scripts.demo --note "$(TEXT)"

seed:
	docker compose exec app python -m scripts.seed

test:
	python -m pytest -q
