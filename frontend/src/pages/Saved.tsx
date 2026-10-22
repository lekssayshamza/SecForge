import { useSearchParams } from 'react-router-dom'
import DiscoveryCatalog from '../components/DiscoveryCatalog'
export default function Saved() {
  const [params] = useSearchParams()
  return <DiscoveryCatalog kind={params.get('tab') === 'paths' ? 'paths' : 'rooms'} saved />
}
