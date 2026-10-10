"""Roles que piensan. Solo hablan por los esquemas del contrato §2; nunca ven secretos.

Los imports pesados viven en cada módulo (orquestador, ejecutor) para no cargarlos al importar
el paquete. El Motor hace `from rookie.roles.ejecutor import ejecutar_tarea` directamente.
"""
