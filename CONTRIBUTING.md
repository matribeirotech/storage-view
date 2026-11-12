# Contribuindo com o Storage View (LYPSYOS OSS)

Obrigado pelo interesse em contribuir com o **Storage View**, projeto mantido pela comunidade **[LYPSYOS Open Source Lab & Community](https://lypsyos.com)**!

## Ambiente de Desenvolvimento

Requer **Python 3.11+**.

```bash
git clone https://github.com/SEU-USUARIO/storage-view.git
cd storage-view
uv sync --all-extras
uv run pytest
```

Ou com `pip`:

```bash
python -m venv .venv
pip install -e ".[dev]"
pytest
```

## Como enviar mudanças

1. Faça um **Fork** deste repositório (`matribeirotech/storage-view`).
2. Crie uma branch para sua feature ou correção:
   ```bash
   git checkout -b feat/minha-melhoria
   ```
3. Teste suas alterações localmente (`pytest` e smoke test CLI `storage-view src --top 10`).
4. Abra um **Pull Request** detalhando claramente o problema resolvido.

## Padrões & Comunidade

- Mantenha o código limpo, tipado, testado e focado em baixa latência e baixo uso de memória.
- Caso encontre um bug, abra uma **Issue** usando o template de relato de bug.
- Procure pelas etiquetas `good first issue` e `help wanted` para contribuições iniciais.
- Ao participar, siga o nosso [Código de Conduta](CODE_OF_CONDUCT.md).
