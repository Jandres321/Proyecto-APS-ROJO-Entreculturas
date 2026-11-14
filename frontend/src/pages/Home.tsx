import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../lib/api";

type Health = { status: string; version: string };

export default function Home() {
  const { data, isError, isLoading } = useQuery({
    queryKey: ["health"],
    queryFn: () => apiGet<Health>("/health"),
    retry: false,
  });

  return (
    <main>
      <h1>Revisor de justificación</h1>
      <p>Herramienta de apoyo a la revisión de la justificación económica de proyectos.</p>
      <p>
        Estado de la API:{" "}
        {isLoading ? "comprobando…" : isError ? "no disponible" : `${data?.status} (v${data?.version})`}
      </p>
      <p>Próximamente: proyectos, incidencias, visor de justificantes.</p>
    </main>
  );
}
