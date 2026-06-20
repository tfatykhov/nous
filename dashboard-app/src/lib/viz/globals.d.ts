declare global {
  interface Window { Chart: any; d3: any; cytoscape: any; }
  const Chart: any; const d3: any; const cytoscape: any;
}
export {};
