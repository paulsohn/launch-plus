import type cytoscape from "cytoscape";

interface Props {
  cyRef: React.RefObject<cytoscape.Core | null>;
}

export function Toolbar({ cyRef }: Props) {
  const handleFit = () => {
    cyRef.current?.fit(undefined, 40);
  };

  const handleCollapseAll = () => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.nodes('[type="group"]').forEach((g) => {
      g.data("_expanded", true);
      (g.children() as unknown as { hide(): void }).hide();
    });
  };

  const handleExpandAll = () => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.nodes('[type="group"]').forEach((g) => {
      g.data("_expanded", false);
      (g.children() as unknown as { show(): void }).show();
    });
  };

  const handleSearch = (query: string) => {
    const cy = cyRef.current;
    if (!cy) return;

    if (!query.trim()) {
      cy.elements().removeClass("highlighted faded");
      return;
    }

    const q = query.trim().toLowerCase();
    cy.elements().addClass("faded");

    const matched = cy.nodes().filter((n) => {
      const d = n.data();
      return (
        d.label?.toLowerCase().includes(q) ||
        d.fqn?.toLowerCase().includes(q) ||
        d.package?.toLowerCase().includes(q) ||
        d.fullName?.toLowerCase().includes(q) ||
        d.name?.toLowerCase().includes(q)
      );
    });

    matched.removeClass("faded").addClass("highlighted");
    matched.ancestors().removeClass("faded");
    matched.connectedEdges().removeClass("faded");
    matched.connectedEdges().connectedNodes().removeClass("faded");
  };

  return (
    <div id="toolbar">
      <span id="title">roscope viz</span>
      <input
        type="text"
        id="search"
        placeholder="Search nodes by name or package..."
        onChange={(e) => handleSearch(e.target.value)}
      />
      <button onClick={handleFit} title="Fit to viewport">
        Fit
      </button>
      <button onClick={handleCollapseAll} title="Collapse all groups">
        Collapse All
      </button>
      <button onClick={handleExpandAll} title="Expand all groups">
        Expand All
      </button>
    </div>
  );
}
