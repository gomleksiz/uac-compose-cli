.PHONY: check check-all lint type test test-corpus fmt publish
check: lint type test
check-all: check test-corpus
lint:
	uv run ruff check src tests scripts
fmt:
	uv run ruff format src tests scripts
type:
	uv run mypy
test:
	uv run pytest
test-corpus:
	uv run pytest -m corpus

# Copy the committed files of PUBLISH_REF into the public checkout as one new
# commit, then push it. The private history never goes public: only the files.
PUBLIC ?= ../uac-compose-public
PUBLIC_REMOTE ?= origin-public
PUBLISH_REF ?= main
publish: check
	@set -e; \
	test -d "$(PUBLIC)/.git" || { echo "publish: no public checkout at $(PUBLIC)" >&2; exit 1; }; \
	git diff --quiet HEAD || { echo "publish: commit your changes first" >&2; exit 1; }; \
	test -z "$$(git -C "$(PUBLIC)" status --porcelain)" || { echo "publish: $(PUBLIC) has changes" >&2; exit 1; }; \
	rev=$$(git rev-parse --short $(PUBLISH_REF)); \
	ver=$$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml); \
	msg="$${MSG:-uac-compose $$ver}"; \
	echo "publish: $(PUBLISH_REF) ($$rev) -> $(PUBLIC) ($(PUBLIC_REMOTE))"; \
	git -C "$(PUBLIC)" pull -q --ff-only $(PUBLIC_REMOTE) main; \
	git -C "$(PUBLIC)" rm -rq --cached .; \
	git -C "$(PUBLIC)" clean -fdq; \
	git archive $(PUBLISH_REF) | tar -x -C "$(PUBLIC)"; \
	git -C "$(PUBLIC)" add -A; \
	if git -C "$(PUBLIC)" diff --cached --quiet; then echo "publish: nothing to publish"; exit 0; fi; \
	git -C "$(PUBLIC)" diff --cached --stat; \
	printf 'Commit "%s" and push? [y/N] ' "$$msg"; read ok; \
	if [ "$$ok" != y ]; then \
		git -C "$(PUBLIC)" reset -q --hard; git -C "$(PUBLIC)" clean -fdq; \
		echo "publish: stopped, $(PUBLIC) is unchanged"; exit 1; \
	fi; \
	git -C "$(PUBLIC)" commit -q -m "$$msg"; \
	git -C "$(PUBLIC)" push -q $(PUBLIC_REMOTE) HEAD:main; \
	echo "publish: pushed $$(git -C "$(PUBLIC)" rev-parse --short HEAD)"
