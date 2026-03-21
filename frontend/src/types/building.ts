export interface BuildingProperties {
  pnu: string;
  building_name: string;
  usage_type: string;
  vintage_class: string;
  built_year: number | null;
  total_area: number | null;
  floors_above: number;
  floors_below: number;
  height: number | null;
  structure_type: string | null;
  energy_grade: string | null;
  total_energy: number | null;
  lng: number | null;
  lat: number | null;
}

export interface BuildingFeature {
  type: "Feature";
  geometry: GeoJSON.Geometry;
  properties: BuildingProperties;
}

export interface BuildingCollection {
  type: "FeatureCollection";
  features: BuildingFeature[];
}

export interface EnergyBreakdown {
  total_energy: number;
  heating: number | null;
  cooling: number | null;
  hot_water: number | null;
  lighting: number | null;
  ventilation: number | null;
}

export interface BuildingDetail extends BuildingFeature {
  properties: BuildingProperties & {
    energy?: EnergyBreakdown;
  };
}

export interface StatsResponse {
  total_count: number;
  avg_energy: number | null;
  grade_distribution: Record<string, number>;
  usage_distribution: Record<string, number>;
}

export interface SearchResult {
  pnu: string;
  building_name: string;
  usage_type: string;
  lng: number | null;
  lat: number | null;
}
