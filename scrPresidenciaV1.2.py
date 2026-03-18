# -*- coding: utf-8 -*-
import time
import hashlib
import os
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from pathlib import Path


'''
********
***************** CONFIGURACIÓN REALIZADA PARA QUE FUNCIONE EN MI MAQUINA.
***************** SE DEBE CAMBIAR
********
'''
# Configuración
URL_PORTAL = "https://minka.presidencia.gob.ec/portal/usuarios_externos.jsf"
ARCHIVO_HASH = r"D:\minka\hash_guardado.txt"
CARPETA_DESCARGAS = "D:\minka\DescargaPDF"  # Ajusta esta ruta
EXTENSION_PDF = ".pdf"
API_UPLOAD = "http://sds0100ap204/SharePointPdfUploader/api/Upload/upload-pdf"
ARCHIVO_DECRETOS = r"D:\minka\decretos_guardados.txt"


# Configurar opciones de Chrome
chrome_options = Options()
chrome_options.add_argument("--headless")  # Ejecutar en segundo plano
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1920,1080")
chrome_options.add_experimental_option("prefs", {
    "download.default_directory": CARPETA_DESCARGAS,
    "download.prompt_for_download": False,
    "download.directory_upgrade": True,
    "plugins.always_open_pdf_externally": True 
})

# ChromeDriver_desde_rutaLocal
#service = Service("D:/proyectos_bdp/minka_vs2022/bin/chromedriver.exe")
#driver = webdriver.Chrome(service=service, options=chrome_options)

'''
********
*****************
***************** 
********
'''

# Funciones
def obtener_contenido_tabla(driver):
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.XPATH, '//form[contains(@id, "frmDataTableDecretosCertificados")]'))
    )
    registros = driver.find_elements(By.XPATH, '//form[contains(@id, "frmDataTableDecretosCertificados")]//span[@title]')
    return "".join([r.text for r in registros])

def obtener_hash_contenido(contenido):
    return hashlib.md5(contenido.encode()).hexdigest()

def guardar_hash(hash_valor, archivo=ARCHIVO_HASH):
    with open(archivo, "w", encoding="utf-8") as f:
        f.write(hash_valor)

def cargar_hash(archivo=ARCHIVO_HASH):
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

def wait_for_new_file(carpeta, before_set, extension=EXTENSION_PDF, timeout=30, poll_interval=0.5):
    """Espera hasta que aparezca un archivo nuevo con la extensión dada. Devuelve ruta completa o None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = set(os.listdir(carpeta))
        added = current - before_set
        # filtrar por extensión y descartar archivos temporales (.crdownload)
        candidates = [f for f in added if f.lower().endswith(extension) and not f.lower().endswith(".crdownload")]
        if candidates:
            # devolver el más reciente por mtime
            rutas = [os.path.join(carpeta, c) for c in candidates]
            rutas_existentes = [r for r in rutas if os.path.isfile(r)]
            if rutas_existentes:
                rutas_existentes.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                return rutas_existentes[0]
        # también comprobar si algún archivo cambió su extensión (fin de descarga)
        # revisar archivos que estaban antes y ahora tienen la extensión completa
        for f in before_set:
            if f.lower().endswith(".crdownload"):
                possible = f[:-len(".crdownload")]
                posible_path = os.path.join(carpeta, possible)
                if os.path.exists(posible_path):
                    return posible_path
        time.sleep(poll_interval)
    return None

def descargar_pdfs_nuevos(driver, registros_nuevos, carpeta_descargas=CARPETA_DESCARGAS, timeout_por_descarga=30):
    """
    Hace clic en el botón de descarga para cada decreto nuevo y espera el archivo resultante.
    Devuelve un dict {Decreto: ruta_archivo_pdf}.
    """
    decretos_nuevos = set(r["Decreto"] for r in registros_nuevos)
    decretos_descargados = {}
    filas = driver.find_elements(By.XPATH, '//form[contains(@id, "frmDataTableDecretosCertificados")]//tbody/tr')
    print(f"Se encontraron {len(filas)} filas en la tabla.")

    for fila in filas:
        columnas = fila.find_elements(By.TAG_NAME, "td")
        if len(columnas) >= 3:
            decreto = columnas[0].text.strip()
            if decreto in decretos_nuevos and decreto not in decretos_descargados:
                try:
                    # estado de archivos antes de la descarga
                    before = set(os.listdir(carpeta_descargas)) if os.path.exists(carpeta_descargas) else set()
                    boton = fila.find_element(By.XPATH, './/button[@title="Descargar Archivo pdf Firmado"]')
                    boton.click()
                    print(f"Descargando PDF del decreto {decreto}...")
                    ruta_nueva = wait_for_new_file(carpeta_descargas, before, extension=EXTENSION_PDF, timeout=timeout_por_descarga)
                    if ruta_nueva:
                        decretos_descargados[decreto] = ruta_nueva
                        print(f"Descarga completada para {decreto}: {ruta_nueva}")
                    else:
                        print(f"[WARN] Timeout esperando PDF para decreto {decreto}")
                    time.sleep(1)  # pequeño descanso antes de seguir
                except Exception as e:
                    print(f"No se pudo descargar el PDF del decreto {decreto}: {e}")
    return decretos_descargados

def obtener_pdfs_recientes(carpeta, extension=EXTENSION_PDF, minutos=5):
    ahora = time.time()
    archivos = []
    for archivo in os.listdir(carpeta):
        if archivo.endswith(extension):
            ruta = os.path.join(carpeta, archivo)
            if os.path.isfile(ruta) and ahora - os.path.getmtime(ruta) < minutos * 60:
                archivos.append(ruta)
    return archivos

def cargar_decretos_guardados():
    try:
        with open(ARCHIVO_DECRETOS, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f.readlines())
    except FileNotFoundError:
        return set()

def guardar_decretos_nuevos(decretos_nuevos):
    ruta = Path(ARCHIVO_DECRETOS)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    if not ruta.exists():
        ruta.write_text("", encoding="utf-8")
        print(f"[INFO] Archivo '{ARCHIVO_DECRETOS}' creado correctamente.")
    else:
        print(f"[INFO] Archivo '{ARCHIVO_DECRETOS}' ya existe.")
    decretos_ordenados = sorted(decretos_nuevos, reverse=True)
    with ruta.open("a", encoding="utf-8") as f:
        for d in decretos_ordenados:
            f.write(str(d).strip() + "\n")
    print(f"[INFO] Se guardaron {len(decretos_ordenados)} decretos en '{ARCHIVO_DECRETOS}'.")

def subir_pdf_con_datos(ruta_pdf, datos):
    with open(ruta_pdf, "rb") as f:
        files = {
            "file": (os.path.basename(ruta_pdf), f, "application/pdf")
        }
        data = {
            "FolderPath": "1. Marco Normativo Externo/1.5. Presidencia de la República",
            "TipoDocumento": "Decreto",
            "Emite": "Presidencia",
            "FechaEmision": datos.get("FechaEmision", ""),
            "Descripcion": datos.get("Descripcion", ""),
            "Decreto": datos.get("Decreto", ""),

            # DATOS QUEMADOS QUE TIENEN QUE CAMBIARSE.
            # ***********
            # *********

            "codSucursal": "1",
            "codOficina":"1",
            "codUsuario": "BDRODRIG",
            "codMaquina": "GYE007",
            "Ip": "10.1.128.12"

            # ***********
            # *********
        }
        try:
            print(f"[DEBUG] Subiendo {ruta_pdf} con datos: {data}")
            response = requests.post(API_UPLOAD, files=files, data=data, timeout=30)
            print(f"[DEBUG] Status: {response.status_code}")
            print(f"[DEBUG] Response body: {response.text}")
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Falló subida de {ruta_pdf}: {e}")
            return False

def extraer_datos_registros(driver):
    filas = driver.find_elements(By.XPATH, '//form[contains(@id, "frmDataTableDecretosCertificados")]//tbody/tr')
    lista_registros = []
    for fila in filas:
        columnas = fila.find_elements(By.TAG_NAME, "td")
        if len(columnas) >= 3:
            Decreto = columnas[0].text.strip()
            Descripcion = columnas[1].text.strip()
            FechaEmision = columnas[2].text.strip()
            lista_registros.append({
                "FechaEmision": FechaEmision,
                "Descripcion": Descripcion,
                "Decreto": Decreto
            })
    return lista_registros

# --- Inicio del script ---
driver = webdriver.Chrome(options=chrome_options)
driver.get(URL_PORTAL)

contenido_actual = obtener_contenido_tabla(driver)
hash_actual = obtener_hash_contenido(contenido_actual)
hash_anterior = cargar_hash()

if hash_actual != hash_anterior:
    print("Nuevo registro detectado")
    guardar_hash(hash_actual)
    try:
        registros_json = extraer_datos_registros(driver)
        decretos_guardados = cargar_decretos_guardados()
        registros_nuevos = [r for r in registros_json if r["Decreto"] not in decretos_guardados]
        if registros_nuevos:
            # Descargar y obtener mapping decreto -> archivo descargado
            mapping = descargar_pdfs_nuevos(driver, registros_nuevos, carpeta_descargas=CARPETA_DESCARGAS, timeout_por_descarga=30)
            time.sleep(2)

            print(f"Mapeo descargas: {mapping}")

            # Construir diccionario de datos por decreto para subir en el orden correcto
            datos_por_decreto = {r["Decreto"]: r for r in registros_nuevos}

            for decreto, ruta_pdf in mapping.items():
                datos = datos_por_decreto.get(decreto)
                if datos:
                    subir_pdf_con_datos(ruta_pdf, datos)
                else:
                    print(f"[WARN] No hay datos asociados para el decreto {decreto}, archivo {ruta_pdf}")

        guardar_decretos_nuevos([r["Decreto"] for r in registros_nuevos])

    except Exception as e:
        print("Error al descargar archivos:", e)
else:
    print("Sin cambios detectados")

driver.quit()

