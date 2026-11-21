import Link from 'next/link'
import Image from 'next/image'
import { formatter } from '../utils/helpers'

const ProductCard = ({ product, showSoldOut = false }) => {
  const { handle, title, availableForSale } = product.node

  const { altText, url } = product.node.images.edges[0].node

  const price = product.node.priceRange.minVariantPrice.amount

  return (
    <Link
      href={`/products/${handle}`}
    >
      <a className="group">
        <div className="w-full overflow-hidden bg-gray-200 rounded-3xl">
          <div className="relative group-hover:opacity-75 h-72">
            <Image 
              src={url}
              alt={altText}
              layout="fill"
              objectFit="cover"
            />
            {showSoldOut && availableForSale === false && (
              <span className="absolute top-3 right-3 px-2 py-1 rounded-md bg-red-600 text-white text-xs font-semibold">
                Sold out
              </span>
            )}
          </div>
        </div>
        <h3 className="mt-4 text-lg font-medium text-gray-900">{title}</h3>
        <p className="mt-1 text-sm text-gray-700">{formatter.format(price)}</p>
      </a>
    </Link>
  )
}

export default ProductCard