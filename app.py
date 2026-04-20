from datetime import datetime, date, timedelta

def formatar_data_br(data_str):
    """Converte qualquer formato (ISO, Texto ou Número do Google) para DD/MM/AAAA"""
    if not data_str or str(data_str).strip() in ["-", "None", ""]: return None
    
    try:
        # CENÁRIO 1: É um número puro do Google Sheets (ex: 46111)
        if str(data_str).replace('.','',1).isdigit():
            dias = int(float(data_str))
            # O Google Sheets conta a partir de 30/12/1899
            data_base = datetime(1899, 12, 30)
            dt = data_base + timedelta(days=dias)
            return dt.strftime('%d/%m/%Y')

        # CENÁRIO 2: É uma string ISO (ex: 2026-05-05T03:00:00Z)
        dt = datetime.fromisoformat(str(data_str).replace('Z', '+00:00'))
        return dt.strftime('%d/%m/%Y')
    except:
        try:
            # CENÁRIO 3: É uma string YYYY-MM-DD
            dt = datetime.strptime(str(data_str)[:10], '%Y-%m-%d')
            return dt.strftime('%d/%m/%Y')
        except:
            # Se tudo falhar, retorna o que veio (evita quebrar o bot)
            return str(data_str)

def converter_para_objeto_data(data_str):
    """Converte para objeto date para cálculos, aceitando o número do Google"""
    if not data_str or str(data_str).strip() in ["-", "None", ""]: return None
    try:
        # Se for número
        if str(data_str).replace('.','',1).isdigit():
            dias = int(float(data_str))
            return (datetime(1899, 12, 30) + timedelta(days=dias)).date()
        
        # Se for ISO
        return datetime.fromisoformat(str(data_str).replace('Z', '+00:00')).date()
    except:
        try:
            return datetime.strptime(str(data_str)[:10], '%Y-%m-%d').date()
        except:
            return None
