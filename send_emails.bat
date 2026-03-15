@echo off
cd /d "C:\Users\CM\Documents\tortugatour (1)\tortugatour\tortugatour"
python manage.py check_agencias_sin_pago_recientes
python manage.py check_agencias_sin_pago_7_dias
