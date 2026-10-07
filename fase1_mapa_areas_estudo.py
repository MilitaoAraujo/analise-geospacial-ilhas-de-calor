"""
Projeto: Análise de Ilhas de Calor Urbanas (ICU) — Campina Grande/PB
Fase 1: Carregamento e visualização das áreas de estudo.

O script monta um GeoDataFrame com as zonas de interesse da pesquisa:
    - Zonas de Teste (ICU) .............. Centro, Catolé
    - Zonas de Controle (Não-ICU) ....... Parque da Criança, Açude de Bodocongó

e gera um mapa interativo (Folium) com camadas separadas por categoria,
popups e tooltips.

Cada área tem duas geometrias:
    - limite ........ contorno da área (buffer aproximado ou malha oficial);
    - amostragem .... recorte usado para extrair pixels nas Fases 2 e 3.
                      Zonas de controle são encolhidas por uma margem interna
                      (evita pixels de borda com asfalto/edificações) e zonas
                      de teste excluem o entorno das zonas de controle.

Fontes de geometria (em ordem de prioridade):
    1. Arquivo local (GeoJSON / Shapefile / GPKG) informado em --arquivo,
       p.ex. a malha de bairros do IBGE (Censo 2022) ou um export do OSM.
    2. Geometrias aproximadas criadas via shapely a partir de coordenadas
       reais (OpenStreetMap/Nominatim) + raio — úteis para testes iniciais.

Uso:
    pip install -r requirements.txt
    python fase1_mapa_areas_estudo.py --abrir
    python fase1_mapa_areas_estudo.py --arquivo dados/bairros_cg.geojson --coluna-nome NM_BAIRRO
"""

from __future__ import annotations

import argparse
import unicodedata
import webbrowser
from pathlib import Path

import folium
import geopandas as gpd
from folium.plugins import Fullscreen, MeasureControl, MiniMap
from shapely.geometry import Point

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

# CRS geográfico (lat/lon) usado pelo Folium/Leaflet.
CRS_WGS84 = "EPSG:4326"
# CRS métrico para buffers e áreas: SIRGAS 2000 / UTM zona 25S
# (Campina Grande fica em ~35,88° W, dentro da zona 25).
CRS_METRICO = "EPSG:31985"

# Centro aproximado do município, usado para centralizar o mapa.
CENTRO_CAMPINA_GRANDE = (-7.2250, -35.8880)

CATEGORIA_TESTE = "Teste (ICU)"
CATEGORIA_CONTROLE = "Controle (Não-ICU / Referência)"

# Faixa (m) em torno das zonas de controle que é removida das zonas de teste,
# para que pixels frios do parque/açude não entrem na média de teste e os dois
# conjuntos de amostras fiquem separados.
DISTANCIA_EXCLUSAO_M = 100

# Resolução do produto de temperatura de superfície do Landsat 8/9 (Collection 2,
# Level-2, banda ST_B10 reamostrada para 30 m; o sensor TIRS é de 100 m nativos).
RESOLUCAO_PIXEL_M = 30
# Abaixo disso a média de referência fica estatisticamente frágil.
PIXELS_MINIMOS_AMOSTRAGEM = 20

# Estilo visual por categoria.
ESTILO_CATEGORIA = {
    CATEGORIA_TESTE: {"cor": "#d7301f", "icone": "fire", "cor_marker": "red"},
    CATEGORIA_CONTROLE: {"cor": "#1a9850", "icone": "tree", "cor_marker": "green"},
}

# Áreas de estudo. Coordenadas (lat, lon) em WGS84 conferidas no OpenStreetMap
# (Nominatim). `raio_m` define o limite aproximado quando não há malha oficial;
# `margem_interna_m` encolhe o limite para gerar a geometria de amostragem.
#
# Dimensões de referência (bounding box no OSM):
#   - Parque da Criança ..... ~280 m (N-S) x ~300 m (L-O), na divisa Centro/Catolé
#   - Açude de Bodocongó .... ~470 m (N-S) x ~1.090 m (L-O), alongado L-O
# Os raios das zonas de controle foram escolhidos para caber na parte
# vegetada / no espelho d'água; confira no mapa de satélite antes da Fase 2.
AREAS_ESTUDO = [
    {
        "nome": "Centro",
        "categoria": CATEGORIA_TESTE,
        "lat": -7.2190,
        "lon": -35.8825,
        "raio_m": 700,
        "margem_interna_m": 0,
        "descricao": "Núcleo comercial adensado, alta impermeabilização "
                     "e baixa cobertura vegetal.",
    },
    {
        "nome": "Catolé",
        "categoria": CATEGORIA_TESTE,
        "lat": -7.2382,
        "lon": -35.8774,
        "raio_m": 800,
        "margem_interna_m": 0,
        "descricao": "Bairro verticalizado e de intenso tráfego, com grandes "
                     "superfícies asfaltadas e edificações.",
    },
    {
        "nome": "Parque da Criança",
        "categoria": CATEGORIA_CONTROLE,
        "lat": -7.2270,
        "lon": -35.8784,
        "raio_m": 130,
        "margem_interna_m": 30,
        "descricao": "Área verde urbana com arborização e solo permeável.",
    },
    {
        "nome": "Açude de Bodocongó",
        "categoria": CATEGORIA_CONTROLE,
        "lat": -7.2140,
        "lon": -35.9156,
        "raio_m": 210,
        "margem_interna_m": 30,
        "descricao": "Espelho d'água com efeito de resfriamento por "
                     "evaporação no entorno.",
    },
]


# ---------------------------------------------------------------------------
# Construção / carregamento dos dados
# ---------------------------------------------------------------------------

def criar_gdf_areas(areas: list[dict] = AREAS_ESTUDO) -> gpd.GeoDataFrame:
    """Cria o GeoDataFrame das áreas de estudo com limites aproximados.

    Cada área vira um círculo (buffer) de `raio_m` metros em torno do ponto
    de referência. O buffer é feito no CRS métrico para que o raio esteja
    realmente em metros, e o resultado é reprojetado para WGS84.
    """
    pontos = gpd.GeoDataFrame(
        areas,
        geometry=[Point(a["lon"], a["lat"]) for a in areas],  # shapely: (x=lon, y=lat)
        crs=CRS_WGS84,
    )
    metrico = pontos.to_crs(CRS_METRICO)
    metrico["geometry"] = metrico.geometry.buffer(metrico["raio_m"])
    metrico["fonte_geometria"] = "Aproximação (buffer)"
    return metrico.to_crs(CRS_WGS84)


def _normalizar(texto: str) -> str:
    """Remove acentos e caixa para comparar nomes de bairros."""
    sem_acento = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return sem_acento.strip().lower()


def carregar_malha_bairros(caminho: str | Path, coluna_nome: str) -> gpd.GeoDataFrame:
    """Lê uma malha de bairros (GeoJSON/Shapefile/GPKG) e garante CRS WGS84."""
    malha = gpd.read_file(caminho)
    if coluna_nome not in malha.columns:
        raise KeyError(
            f"Coluna '{coluna_nome}' não encontrada. Colunas disponíveis: {list(malha.columns)}"
        )
    if malha.crs is None:
        malha = malha.set_crs(CRS_WGS84)
    return malha.to_crs(CRS_WGS84)


def substituir_por_malha(
    areas: gpd.GeoDataFrame, malha: gpd.GeoDataFrame, coluna_nome: str
) -> gpd.GeoDataFrame:
    """Troca o limite aproximado pelo da malha oficial quando o nome bate.

    Áreas que não são bairros (ex.: Parque da Criança, Açude de Bodocongó)
    normalmente não existem na malha e permanecem com o buffer aproximado.
    """
    indice = {_normalizar(n): geom for n, geom in zip(malha[coluna_nome], malha.geometry)}
    areas = areas.copy()
    for i, nome in areas["nome"].items():
        geom = indice.get(_normalizar(nome))
        if geom is not None:
            areas.at[i, "geometry"] = geom
            areas.at[i, "fonte_geometria"] = "Malha de bairros"
    return areas


def criar_geometrias_amostragem(areas: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Gera a geometria de amostragem de cada área (usada nas Fases 2 e 3).

    - Controle: limite encolhido por `margem_interna_m`, descartando pixels de
      borda (ruas, calçadas e edificações vizinhas) que contaminariam T_ref.
    - Teste: limite menos as zonas de controle ampliadas em
      DISTANCIA_EXCLUSAO_M. Isso também resolve o caso da malha oficial, em
      que o Parque da Criança fica dentro/na divisa do polígono do Catolé.

    As zonas de controle têm prioridade: nunca são recortadas pelas de teste.
    """
    metrico = areas.to_crs(CRS_METRICO)
    e_controle = metrico["categoria"] == CATEGORIA_CONTROLE

    amostragem = metrico.geometry.buffer(-metrico["margem_interna_m"])
    exclusao = amostragem[e_controle].union_all().buffer(DISTANCIA_EXCLUSAO_M)
    amostragem[~e_controle] = amostragem[~e_controle].difference(exclusao)

    metrico["area_limite_km2"] = metrico.geometry.area / 1e6
    metrico["area_amostragem_km2"] = amostragem.area / 1e6
    metrico["pixels_estimados"] = (amostragem.area / RESOLUCAO_PIXEL_M**2).round().astype(int)
    resultado = metrico.to_crs(CRS_WGS84)
    resultado["geom_amostragem"] = amostragem.to_crs(CRS_WGS84)
    return resultado


def validar_areas(areas: gpd.GeoDataFrame) -> list[str]:
    """Verifica sobreposições e tamanho mínimo das amostras; retorna avisos."""
    avisos = []
    metrico = areas.to_crs(CRS_METRICO)
    amostragem = gpd.GeoSeries(areas["geom_amostragem"], crs=CRS_WGS84).to_crs(CRS_METRICO)
    controles = metrico.index[metrico["categoria"] == CATEGORIA_CONTROLE]
    testes = metrico.index[metrico["categoria"] == CATEGORIA_TESTE]

    for c in controles:
        nome_c = metrico.at[c, "nome"]
        for t in testes:
            nome_t = metrico.at[t, "nome"]
            if metrico.geometry[c].intersects(metrico.geometry[t]):
                avisos.append(
                    f"Limite de '{nome_c}' toca/sobrepõe '{nome_t}' "
                    f"(tratado na amostragem com exclusão de {DISTANCIA_EXCLUSAO_M} m)."
                )
            distancia = amostragem[c].distance(amostragem[t])
            if distancia < DISTANCIA_EXCLUSAO_M - 1:  # tolerância de arredondamento
                avisos.append(
                    f"Amostragem de '{nome_c}' está a {distancia:.0f} m de '{nome_t}' "
                    f"(mínimo: {DISTANCIA_EXCLUSAO_M} m)."
                )
        pixels = metrico.at[c, "pixels_estimados"]
        if pixels < PIXELS_MINIMOS_AMOSTRAGEM:
            avisos.append(
                f"Amostragem de '{nome_c}' tem só ~{pixels} pixels de {RESOLUCAO_PIXEL_M} m "
                f"(mínimo recomendado: {PIXELS_MINIMOS_AMOSTRAGEM})."
            )
    return avisos


# ---------------------------------------------------------------------------
# Visualização
# ---------------------------------------------------------------------------

def criar_mapa_base(centro: tuple[float, float] = CENTRO_CAMPINA_GRANDE) -> folium.Map:
    """Cria o mapa base com camadas de fundo alternáveis."""
    mapa = folium.Map(location=centro, zoom_start=14, tiles=None, control_scale=True)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(mapa)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        attr="Esri — World Light Gray Canvas",
        name="Cinza claro (Esri)",
    ).add_to(mapa)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri — World Imagery",
        name="Satélite (Esri)",
    ).add_to(mapa)
    return mapa


def _html_popup(linha) -> str:
    """Monta o HTML do popup de uma área."""
    cor = ESTILO_CATEGORIA[linha["categoria"]]["cor"]
    return f"""
    <div style="font-family: sans-serif; min-width: 230px">
      <h4 style="margin: 0 0 4px 0">{linha['nome']}</h4>
      <span style="color: {cor}; font-weight: bold">{linha['categoria']}</span>
      <p style="margin: 6px 0">{linha['descricao']}</p>
      <small>
        Lat/Lon: {linha['lat']:.4f}, {linha['lon']:.4f}<br>
        Área do limite: {linha['area_limite_km2']:.3f} km²<br>
        Área de amostragem: {linha['area_amostragem_km2']:.3f} km²
        (~{linha['pixels_estimados']} pixels de {RESOLUCAO_PIXEL_M} m)<br>
        Geometria: {linha['fonte_geometria']}
      </small>
    </div>
    """


def adicionar_camada_categoria(
    mapa: folium.Map, gdf: gpd.GeoDataFrame, categoria: str
) -> folium.FeatureGroup:
    """Adiciona limites, amostragem e marcadores de uma categoria num FeatureGroup."""
    estilo = ESTILO_CATEGORIA[categoria]
    cor = estilo["cor"]
    grupo = folium.FeatureGroup(name=f"Zonas de {categoria}", show=True)

    for _, linha in gdf[gdf["categoria"] == categoria].iterrows():
        tooltip = f"{linha['nome']} — {categoria}"

        # Limite da área: só contorno tracejado.
        folium.GeoJson(
            linha.geometry.__geo_interface__,
            style_function=lambda _f, c=cor: {
                "color": c, "weight": 1.5, "dashArray": "6 4", "fillOpacity": 0,
            },
            tooltip=f"{tooltip} (limite)",
        ).add_to(grupo)

        # Geometria de amostragem: área preenchida (o que entra nas médias).
        if not linha["geom_amostragem"].is_empty:
            folium.GeoJson(
                linha["geom_amostragem"].__geo_interface__,
                style_function=lambda _f, c=cor: {
                    "color": c, "weight": 2, "fillColor": c, "fillOpacity": 0.3,
                },
                highlight_function=lambda _f: {"weight": 4, "fillOpacity": 0.5},
                tooltip=f"{tooltip} (amostragem)",
                popup=folium.Popup(_html_popup(linha), max_width=320),
            ).add_to(grupo)

        # Marcador no ponto de referência.
        folium.Marker(
            location=(linha["lat"], linha["lon"]),
            tooltip=tooltip,
            popup=folium.Popup(_html_popup(linha), max_width=320),
            icon=folium.Icon(color=estilo["cor_marker"], icon=estilo["icone"], prefix="fa"),
        ).add_to(grupo)

    grupo.add_to(mapa)
    return grupo


def adicionar_legenda(mapa: folium.Map) -> None:
    """Insere uma legenda fixa no canto inferior esquerdo."""
    itens = "".join(
        f'<div><span style="display:inline-block;width:12px;height:12px;'
        f'background:{e["cor"]};margin-right:6px;opacity:.8"></span>{cat}</div>'
        for cat, e in ESTILO_CATEGORIA.items()
    )
    html = f"""
    <div style="position: fixed; bottom: 30px; left: 30px; z-index: 9999;
                background: white; padding: 10px 12px; border-radius: 6px;
                box-shadow: 0 1px 4px rgba(0,0,0,.3); font: 13px sans-serif">
      <b>ICU — Campina Grande/PB</b>{itens}
      <div style="margin-top: 6px; color: #555">
        Preenchido: amostragem<br>Tracejado: limite
      </div>
    </div>
    """
    mapa.get_root().html.add_child(folium.Element(html))


def gerar_mapa(gdf: gpd.GeoDataFrame) -> folium.Map:
    """Monta o mapa completo: base, camadas por categoria, controles e legenda."""
    mapa = criar_mapa_base()
    for categoria in ESTILO_CATEGORIA:
        adicionar_camada_categoria(mapa, gdf, categoria)

    folium.LayerControl(collapsed=False).add_to(mapa)
    Fullscreen().add_to(mapa)
    MeasureControl(primary_length_unit="meters", primary_area_unit="sqmeters").add_to(mapa)
    MiniMap(toggle_display=True).add_to(mapa)
    adicionar_legenda(mapa)

    # Enquadra todas as áreas de estudo.
    minx, miny, maxx, maxy = gdf.total_bounds
    mapa.fit_bounds([[miny, minx], [maxy, maxx]], padding=(20, 20))
    return mapa


# ---------------------------------------------------------------------------
# Exportação
# ---------------------------------------------------------------------------

def exportar_geojson(areas: gpd.GeoDataFrame, pasta: Path) -> None:
    """Salva limites e geometrias de amostragem em GeoJSON separados.

    As Fases 2 e 3 devem extrair pixels de `areas_amostragem.geojson`.
    """
    atributos = areas.drop(columns=["geometry", "geom_amostragem"])
    limites = gpd.GeoDataFrame(atributos, geometry=areas.geometry, crs=CRS_WGS84)
    amostragem = gpd.GeoDataFrame(atributos, geometry=areas["geom_amostragem"], crs=CRS_WGS84)
    limites.to_file(pasta / "areas_limites.geojson", driver="GeoJSON")
    amostragem.to_file(pasta / "areas_amostragem.geojson", driver="GeoJSON")


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fase 1 — mapa das áreas de estudo de ICU.")
    parser.add_argument("--arquivo", help="Malha de bairros (GeoJSON/Shapefile/GPKG), opcional.")
    parser.add_argument("--coluna-nome", default="NM_BAIRRO",
                        help="Coluna com o nome do bairro na malha (padrão: NM_BAIRRO, IBGE).")
    parser.add_argument("--saida", default="saida/mapa_areas_estudo.html",
                        help="Caminho do HTML gerado.")
    parser.add_argument("--abrir", action="store_true", help="Abre o mapa no navegador ao final.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    areas = criar_gdf_areas()
    if args.arquivo:
        malha = carregar_malha_bairros(args.arquivo, args.coluna_nome)
        areas = substituir_por_malha(areas, malha, args.coluna_nome)
    areas = criar_geometrias_amostragem(areas)

    colunas = ["nome", "categoria", "fonte_geometria",
               "area_limite_km2", "area_amostragem_km2", "pixels_estimados"]
    print(areas[colunas].to_string(index=False, float_format="{:.3f}".format))

    avisos = validar_areas(areas)
    print("\nVerificação das áreas:")
    print("\n".join(f"  [aviso] {a}" for a in avisos) if avisos else "  OK — sem sobreposições.")

    saida = Path(args.saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    exportar_geojson(areas, saida.parent)
    gerar_mapa(areas).save(saida)
    print(f"\nMapa salvo em: {saida.resolve()}")

    if args.abrir:
        webbrowser.open(saida.resolve().as_uri())


if __name__ == "__main__":
    main()
