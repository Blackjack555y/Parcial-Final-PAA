## Qué cambia

<!-- Resumen breve del cambio y por qué. -->

## Tipo de PR

- [ ] `feature/*` → `dev` (nueva funcionalidad)
- [ ] `dev` → `test` (promoción)
- [ ] `test` → `prod` (promoción)

## Fase del plan

<!-- Fase 0–7 de FASES-DESARROLLO.md -->

## Verificación

- [ ] `./scripts/dev.sh lint` sin errores
- [ ] `./scripts/dev.sh test all` en verde
- [ ] Si toca Terraform: revisé el `terraform plan` del workspace destino (comentario del check `terraform-plan`)
- [ ] Sin secretos ni `.tfstate` en el diff
