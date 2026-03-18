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
from selenium.common.exceptions import (
    NoSuchElementException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
)
from pathlib import Path
import logging

# ─── LOGGING ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scrPresidencia.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─── CONFIGURACIÓN ──────────────────────────────────────────────────────────────
URL_PORTAL = "https://minka.presidencia.gob.ec/portal/usuarios_externos.jsf"
# FIX: usar raw-string para evitar el SyntaxWarning de \m
ARCHIVO_HASH = r"D:\minka\hash_guardado.txt"
CARPETA_DESCARGAS = r"D:\minka\DescargaPDF"
EXTENSION_PDF = ".pdf"
API_UPLOAD = "http://sds0100ap204/SharePointPdfUploader/api/Upload/upload-pdf"
ARCHIVO_DECRETOS = r"D:\minka\decretos_guardados.txt"

# Delay en segundos entre cada subida a SharePoint.
# Power Automate usa polling (cada 1-3 min), un delay de 60-90s entre subidas
# le da tiempo al trigger "Cuando se crea un archivo" de detectar cada uno.
DELAY_ENTRE_SUBIDAS_SEG = 90

# Máximo de páginas a recorrer (seguridad para no entrar en bucle infinito)
MAX_PAGINAS = 50

# ─── CHROME OPTIONS ─────────────────────────────────────────────────────────────
chrome_options = Options()
chrome_options.add_argument("--headless")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1920,1080")
chrome_options.add_experimental_option("prefs", {
    "download.default_directory": CARPETA_DESCARGAS,
    "download.prompt_for_download": False,
    "download.directory_upgrade": True,
    "plugins.always_open_pdf_externally": True,
})

# ─── FUNCIONES UTILITARIAS ──────────────────────────────────────────────────────

def obtener_hash_contenido(contenido: str) -> str:
    return hashlib.md5(contenido.encode()).hexdigest()


def guardar_hash(hash_valor: str, archivo: str = ARCHIVO_HASH):
    Path(archivo).parent.mkdir(parents=True, exist_ok=True)
    with open(archivo, "w", encoding="utf-8") as f:
        f.write(hash_valor)


def cargar_hash(archivo: str = ARCHIVO_HASH):
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def cargar_decretos_guardados() -> set:
    try:
        with open(ARCHIVO_DECRETOS, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def guardar_decretos_nuevos(decretos: list):
    ruta = Path(ARCHIVO_DECRETOS)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    if not ruta.exists():
        ruta.write_text("", encoding="utf-8")
        log.info(f"Archivo '{ARCHIVO_DECRETOS}' creado.")
    decretos_ordenados = sorted(decretos, reverse=True)
    with ruta.open("a", encoding="utf-8") as f:
        for d in decretos_ordenados:
            f.write(str(d).strip() + "\n")
    log.info(f"Se guardaron {len(decretos_ordenados)} decretos en '{ARCHIVO_DECRETOS}'.")


# ─── FUNCIONES DE PAGINACIÓN Y EXTRACCIÓN ───────────────────────────────────────

def esperar_tabla(driver, timeout: int = 20):
    """Espera a que la tabla de decretos esté presente en el DOM."""
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located(
            (By.XPATH, '//form[contains(@id, "frmDataTableDecretosCertificados")]//tbody')
        )
    )


def obtener_contenido_tabla_pagina(driver) -> str:
    """Devuelve texto concatenado de los spans con @title de la página actual."""
    registros = driver.find_elements(
        By.XPATH,
        '//form[contains(@id, "frmDataTableDecretosCertificados")]//span[@title]',
    )
    return "".join(r.text for r in registros)


def extraer_datos_pagina(driver) -> list[dict]:
    """Extrae los registros de la página actual de la tabla."""
    filas = driver.find_elements(
        By.XPATH,
        '//form[contains(@id, "frmDataTableDecretosCertificados")]//tbody/tr',
    )
    registros = []
    for fila in filas:
        cols = fila.find_elements(By.TAG_NAME, "td")
        if len(cols) >= 3:
            registros.append({
                "Decreto": cols[0].text.strip(),
                "Descripcion": cols[1].text.strip(),
                "FechaEmision": cols[2].text.strip(),
            })
    return registros


def hay_pagina_siguiente(driver) -> bool:
    """
    Detecta si existe un botón de 'siguiente página' habilitado.
    PrimeFaces genera un paginador con clases 'ui-paginator-next'.
    Si el botón tiene la clase 'ui-state-disabled', no hay más páginas.
    """
    try:
        btn = driver.find_element(
            By.XPATH,
            '//form[contains(@id, "frmDataTableDecretosCertificados")]'
            '//a[contains(@class, "ui-paginator-next")]',
        )
        clases = btn.get_attribute("class") or ""
        return "ui-state-disabled" not in clases
    except NoSuchElementException:
        # Intentar variantes comunes de paginadores PrimeFaces
        try:
            btn = driver.find_element(
                By.XPATH,
                '//span[contains(@class, "ui-paginator-next") or contains(@class, "pi-caret-right")]'
                '/ancestor-or-self::a | //a[contains(@class, "ui-paginator-next")]',
            )
            clases = btn.get_attribute("class") or ""
            return "ui-state-disabled" not in clases
        except NoSuchElementException:
            return False


def ir_pagina_siguiente(driver, timeout: int = 15):
    """
    Hace clic en 'siguiente página' y espera a que la tabla se refresque.
    PrimeFaces usa AJAX, así que esperamos a que las filas cambien.
    """
    # Capturar el texto de la primera fila antes del clic
    try:
        primera_fila_antes = driver.find_element(
            By.XPATH,
            '//form[contains(@id, "frmDataTableDecretosCertificados")]//tbody/tr[1]/td[1]',
        ).text
    except Exception:
        primera_fila_antes = None

    # Buscar y hacer clic en el botón siguiente
    btn = driver.find_element(
        By.XPATH,
        '//form[contains(@id, "frmDataTableDecretosCertificados")]'
        '//a[contains(@class, "ui-paginator-next")]',
    )
    try:
        btn.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", btn)

    # Esperar a que la tabla se actualice (la primera fila debe cambiar)
    if primera_fila_antes:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                primera_fila_actual = driver.find_element(
                    By.XPATH,
                    '//form[contains(@id, "frmDataTableDecretosCertificados")]//tbody/tr[1]/td[1]',
                ).text
                if primera_fila_actual != primera_fila_antes:
                    break
            except StaleElementReferenceException:
                pass  # El DOM se está refrescando
            time.sleep(0.5)
    else:
        time.sleep(3)  # fallback

    # Esperar a que la tabla esté lista
    esperar_tabla(driver)
    time.sleep(1)  # pequeña pausa adicional para estabilidad AJAX


def extraer_todos_los_registros(driver) -> list[dict]:
    """
    FIX PROBLEMA 1: Recorre TODAS las páginas de la tabla y devuelve todos los registros.
    """
    todos = []
    pagina = 1

    esperar_tabla(driver)
    registros = extraer_datos_pagina(driver)
    todos.extend(registros)
    log.info(f"Página {pagina}: {len(registros)} registros extraídos.")

    while hay_pagina_siguiente(driver) and pagina < MAX_PAGINAS:
        pagina += 1
        try:
            ir_pagina_siguiente(driver)
            registros = extraer_datos_pagina(driver)
            todos.extend(registros)
            log.info(f"Página {pagina}: {len(registros)} registros extraídos.")
            if not registros:
                log.warning("Página sin registros, deteniendo paginación.")
                break
        except Exception as e:
            log.error(f"Error al navegar a página {pagina}: {e}")
            break

    log.info(f"Total de registros extraídos en {pagina} página(s): {len(todos)}")
    return todos


def obtener_contenido_completo(driver) -> str:
    """
    Recorre todas las páginas para generar el hash del contenido completo.
    Después vuelve a la primera página.
    """
    contenido_total = ""
    pagina = 1

    esperar_tabla(driver)
    contenido_total += obtener_contenido_tabla_pagina(driver)

    while hay_pagina_siguiente(driver) and pagina < MAX_PAGINAS:
        pagina += 1
        try:
            ir_pagina_siguiente(driver)
            contenido_total += obtener_contenido_tabla_pagina(driver)
        except Exception:
            break

    # Volver a la primera página
    volver_primera_pagina(driver)
    return contenido_total


def volver_primera_pagina(driver):
    """Navega de vuelta a la primera página del paginador."""
    try:
        btn_first = driver.find_element(
            By.XPATH,
            '//form[contains(@id, "frmDataTableDecretosCertificados")]'
            '//a[contains(@class, "ui-paginator-first")]',
        )
        if "ui-state-disabled" not in (btn_first.get_attribute("class") or ""):
            try:
                btn_first.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", btn_first)
            esperar_tabla(driver)
            time.sleep(1)
    except NoSuchElementException:
        # Si no hay botón first, recargar la página
        driver.get(URL_PORTAL)
        esperar_tabla(driver)
        time.sleep(2)


# ─── DESCARGA DE PDFs ────────────────────────────────────────────────────────────

def wait_for_new_file(carpeta, before_set, extension=EXTENSION_PDF, timeout=30, poll_interval=0.5):
    """Espera hasta que aparezca un archivo nuevo con la extensión dada."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = set(os.listdir(carpeta))
        added = current - before_set
        candidates = [
            f for f in added
            if f.lower().endswith(extension) and not f.lower().endswith(".crdownload")
        ]
        if candidates:
            rutas = [os.path.join(carpeta, c) for c in candidates]
            rutas_existentes = [r for r in rutas if os.path.isfile(r)]
            if rutas_existentes:
                rutas_existentes.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                return rutas_existentes[0]
        for f in before_set:
            if f.lower().endswith(".crdownload"):
                possible = f[: -len(".crdownload")]
                posible_path = os.path.join(carpeta, possible)
                if os.path.exists(posible_path):
                    return posible_path
        time.sleep(poll_interval)
    return None


def descargar_pdfs_nuevos(driver, registros_nuevos, carpeta_descargas=CARPETA_DESCARGAS, timeout_por_descarga=30):
    """
    FIX PROBLEMA 1 (parte 2): Recorre TODAS las páginas de la tabla para buscar
    los decretos que necesita descargar. No se limita a las 10 filas visibles.
    """
    # Asegurarse de que la carpeta de descargas existe
    Path(carpeta_descargas).mkdir(parents=True, exist_ok=True)

    decretos_pendientes = {r["Decreto"] for r in registros_nuevos}
    decretos_descargados = {}
    pagina = 0

    # Volver a la primera página antes de iniciar descargas
    volver_primera_pagina(driver)

    while decretos_pendientes and pagina < MAX_PAGINAS:
        pagina += 1
        esperar_tabla(driver)

        filas = driver.find_elements(
            By.XPATH,
            '//form[contains(@id, "frmDataTableDecretosCertificados")]//tbody/tr',
        )
        log.info(f"Descarga - Página {pagina}: {len(filas)} filas encontradas. Pendientes: {len(decretos_pendientes)}")

        for fila in filas:
            try:
                cols = fila.find_elements(By.TAG_NAME, "td")
                if len(cols) < 3:
                    continue
                decreto = cols[0].text.strip()

                if decreto not in decretos_pendientes:
                    continue

                before = set(os.listdir(carpeta_descargas)) if os.path.exists(carpeta_descargas) else set()
                boton = fila.find_element(By.XPATH, './/button[@title="Descargar Archivo pdf Firmado"]')

                try:
                    boton.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", boton)

                log.info(f"Descargando PDF del decreto {decreto}...")
                ruta_nueva = wait_for_new_file(carpeta_descargas, before, timeout=timeout_por_descarga)

                if ruta_nueva:
                    decretos_descargados[decreto] = ruta_nueva
                    decretos_pendientes.discard(decreto)
                    log.info(f"Descarga completada: {decreto} -> {ruta_nueva}")
                else:
                    log.warning(f"Timeout esperando PDF para decreto {decreto}")

                time.sleep(1)

            except StaleElementReferenceException:
                log.warning(f"DOM cambió al procesar fila, reintentando página {pagina}")
                break
            except Exception as e:
                log.error(f"Error descargando decreto: {e}")

        # Si aún quedan pendientes y hay siguiente página, avanzar
        if decretos_pendientes and hay_pagina_siguiente(driver):
            try:
                ir_pagina_siguiente(driver)
            except Exception as e:
                log.error(f"Error al avanzar página para descargas: {e}")
                break
        else:
            break

    if decretos_pendientes:
        log.warning(f"No se encontraron en la tabla: {decretos_pendientes}")

    return decretos_descargados


# ─── SUBIDA A SHAREPOINT ─────────────────────────────────────────────────────────

def subir_pdf_con_datos(ruta_pdf: str, datos: dict) -> dict | None:
    """
    Sube un PDF a SharePoint vía la API.
    Devuelve el JSON de respuesta (contiene itemId) o None si falla.
    """
    with open(ruta_pdf, "rb") as f:
        files = {
            "file": (os.path.basename(ruta_pdf), f, "application/pdf"),
        }
        data = {
            "FolderPath": "1. Marco Normativo Externo/1.5. Presidencia de la República",
            "TipoDocumento": "Decreto",
            "Emite": "Presidencia",
            "FechaEmision": datos.get("FechaEmision", ""),
            "Descripcion": datos.get("Descripcion", ""),
            "Decreto": datos.get("Decreto", ""),
            # DATOS QUEMADOS — cambiar según ambiente
            "codSucursal": "1",
            "codOficina": "1",
            "codUsuario": "BDRODRIG",
            "codMaquina": "GYE007",
            "Ip": "10.1.128.12",
        }
        try:
            log.info(f"Subiendo {os.path.basename(ruta_pdf)} | Decreto: {datos.get('Decreto')}")
            log.debug(f"Datos: {data}")
            response = requests.post(API_UPLOAD, files=files, data=data, timeout=60)
            log.info(f"Status: {response.status_code} | Body: {response.text}")
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            log.error(f"Falló subida de {ruta_pdf}: {e}")
            return None


def subir_todos_los_pdfs(mapping: dict, datos_por_decreto: dict):
    """
    FIX PROBLEMA 2 y 3: Sube los PDFs uno por uno con un delay configurable
    entre cada subida para que:
      - La API de SharePoint procese cada archivo independientemente
      - Power Automate tenga tiempo de detectar cada trigger individual
    """
    total = len(mapping)
    exitosos = 0
    fallidos = 0

    for i, (decreto, ruta_pdf) in enumerate(mapping.items(), start=1):
        datos = datos_por_decreto.get(decreto)
        if not datos:
            log.warning(f"No hay datos asociados para decreto {decreto}, archivo {ruta_pdf}")
            continue

        log.info(f"[{i}/{total}] Subiendo decreto {decreto}...")
        resultado = subir_pdf_con_datos(ruta_pdf, datos)

        if resultado:
            exitosos += 1
            item_id = resultado.get("itemId", "desconocido")
            log.info(f"[{i}/{total}] Subido OK. ItemId: {item_id}")
        else:
            fallidos += 1
            log.error(f"[{i}/{total}] FALLÓ la subida del decreto {decreto}")

        # FIX PROBLEMA 3: Esperar entre subidas para que Power Automate
        # detecte cada archivo como un evento individual
        if i < total:
            log.info(
                f"Esperando {DELAY_ENTRE_SUBIDAS_SEG}s antes de la siguiente subida "
                f"(para trigger de Power Automate)..."
            )
            time.sleep(DELAY_ENTRE_SUBIDAS_SEG)

    log.info(f"Resumen subida: {exitosos} exitosos, {fallidos} fallidos de {total} total.")
    return exitosos, fallidos


# ─── INICIO DEL SCRIPT ──────────────────────────────────────────────────────────

def main():
    # Crear carpeta de descargas si no existe
    Path(CARPETA_DESCARGAS).mkdir(parents=True, exist_ok=True)

    driver = webdriver.Chrome(options=chrome_options)
    try:
        driver.get(URL_PORTAL)
        log.info(f"Navegando a {URL_PORTAL}")

        # FIX: Obtener hash del contenido COMPLETO (todas las páginas)
        contenido_actual = obtener_contenido_completo(driver)
        hash_actual = obtener_hash_contenido(contenido_actual)
        hash_anterior = cargar_hash()

        if hash_actual == hash_anterior:
            log.info("Sin cambios detectados en la tabla.")
            return

        log.info("Nuevo contenido detectado en la tabla.")
        guardar_hash(hash_actual)

        # Extraer todos los registros (todas las páginas)
        todos_registros = extraer_todos_los_registros(driver)
        decretos_guardados = cargar_decretos_guardados()

        registros_nuevos = [r for r in todos_registros if r["Decreto"] not in decretos_guardados]
        log.info(f"Registros nuevos encontrados: {len(registros_nuevos)}")

        if not registros_nuevos:
            log.info("No hay decretos nuevos para procesar.")
            guardar_hash(hash_actual)
            return

        # Descargar PDFs (navega todas las páginas de la tabla)
        mapping = descargar_pdfs_nuevos(driver, registros_nuevos)
        time.sleep(2)
        log.info(f"Mapeo descargas: {mapping}")

        # Subir a SharePoint con delay entre cada uno
        datos_por_decreto = {r["Decreto"]: r for r in registros_nuevos}
        subir_todos_los_pdfs(mapping, datos_por_decreto)

        # Guardar decretos procesados
        guardar_decretos_nuevos([r["Decreto"] for r in registros_nuevos])

    except Exception as e:
        log.exception(f"Error general: {e}")
    finally:
        driver.quit()
        log.info("Navegador cerrado.")


if __name__ == "__main__":
    main()
