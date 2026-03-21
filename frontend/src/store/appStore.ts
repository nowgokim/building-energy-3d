import { create } from "zustand";
import type { BuildingDetail, StatsResponse } from "../types/building";

interface AppState {
  // Selection
  selectedPnu: string | null;
  selectedBuilding: BuildingDetail | null;
  isPanelOpen: boolean;
  isLoadingDetail: boolean;

  // Stats
  stats: StatsResponse | null;

  // Notifications
  error: string | null;

  // Filters
  filters: {
    energyGrades: string[];
    vintageClasses: string[];
    usageTypes: string[];
  };

  // Actions
  selectBuilding: (pnu: string, detail: BuildingDetail) => void;
  clearSelection: () => void;
  setLoadingDetail: (v: boolean) => void;
  setStats: (stats: StatsResponse) => void;
  setError: (msg: string | null) => void;
  setFilters: (filters: Partial<AppState["filters"]>) => void;
}

export const useAppStore = create<AppState>((set) => ({
  selectedPnu: null,
  selectedBuilding: null,
  isPanelOpen: false,
  isLoadingDetail: false,
  stats: null,
  error: null,
  filters: {
    energyGrades: [],
    vintageClasses: [],
    usageTypes: [],
  },

  selectBuilding: (pnu, detail) =>
    set({
      selectedPnu: pnu,
      selectedBuilding: detail,
      isPanelOpen: true,
      isLoadingDetail: false,
    }),

  clearSelection: () =>
    set({ selectedPnu: null, selectedBuilding: null, isPanelOpen: false }),

  setLoadingDetail: (v) => set({ isLoadingDetail: v }),

  setStats: (stats) => set({ stats }),

  setError: (msg) => {
    set({ error: msg });
    if (msg) {
      setTimeout(() => set({ error: null }), 4000);
    }
  },

  setFilters: (partial) =>
    set((state) => ({
      filters: { ...state.filters, ...partial },
    })),
}));
