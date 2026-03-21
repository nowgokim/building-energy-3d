import { create } from "zustand";
import type { BuildingDetail, StatsResponse } from "../types/building";

interface AppState {
  // Selection
  selectedPnu: string | null;
  selectedBuilding: BuildingDetail | null;
  isPanelOpen: boolean;

  // Stats
  stats: StatsResponse | null;

  // Filters
  filters: {
    energyGrades: string[];
    vintageClasses: string[];
    usageTypes: string[];
  };

  // Actions
  selectBuilding: (pnu: string, detail: BuildingDetail) => void;
  clearSelection: () => void;
  setStats: (stats: StatsResponse) => void;
  setFilters: (filters: Partial<AppState["filters"]>) => void;
}

export const useAppStore = create<AppState>((set) => ({
  selectedPnu: null,
  selectedBuilding: null,
  isPanelOpen: false,
  stats: null,
  filters: {
    energyGrades: [],
    vintageClasses: [],
    usageTypes: [],
  },

  selectBuilding: (pnu, detail) =>
    set({ selectedPnu: pnu, selectedBuilding: detail, isPanelOpen: true }),

  clearSelection: () =>
    set({ selectedPnu: null, selectedBuilding: null, isPanelOpen: false }),

  setStats: (stats) => set({ stats }),

  setFilters: (partial) =>
    set((state) => ({
      filters: { ...state.filters, ...partial },
    })),
}));
