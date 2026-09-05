/* Panel geometry — the single source of truth for the stylesheet (published
   as CSS variables on the root) and for the map's fit paddings. Sizes follow
   the interface scale, but on a narrow viewport the panels cede width so the
   map strip between them can still hold the whole network: MapLibre will not
   zoom out past "the world covers the viewport height", so a strip narrower
   than ~40% of the screen cannot show a global network at any zoom. */

export interface PanelGeometry {
  /** 1rem in CSS px at this scale. */
  unit: number;
  left: number;
  right: number;
  gutter: number;
}

const LEFT_REM = 23;
const RIGHT_REM = 27;
const GUTTER_REM = 0.75;
const LEFT_MAX_VW = 0.25;
const RIGHT_MAX_VW = 0.28;

export function panelGeometry(scale: number, viewportWidth: number): PanelGeometry {
  const unit = 16 * scale;
  return {
    unit,
    left: Math.round(Math.min(LEFT_REM * unit, LEFT_MAX_VW * viewportWidth)),
    right: Math.round(Math.min(RIGHT_REM * unit, RIGHT_MAX_VW * viewportWidth)),
    gutter: GUTTER_REM * unit,
  };
}

/** Write the geometry onto the root element for the stylesheet to consume. */
export function applyPanelGeometry(geometry: PanelGeometry): void {
  const style = document.documentElement.style;
  style.fontSize = `${geometry.unit}px`;
  style.setProperty("--panel-left", `${geometry.left}px`);
  style.setProperty("--panel-right", `${geometry.right}px`);
}
