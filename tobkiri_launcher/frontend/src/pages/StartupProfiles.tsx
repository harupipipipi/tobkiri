import { Navigate, useLocation } from 'react-router';
import { panelRoutes } from '@/src/lib/routes';

export function StartupProfiles() {
  const location = useLocation();
  return <Navigate to={`${panelRoutes.profileGraph}${location.search}${location.hash}`} replace />;
}
