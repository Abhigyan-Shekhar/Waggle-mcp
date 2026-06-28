import React, { useState, useRef, useEffect, useLayoutEffect, useMemo } from "react";

export function VirtualList({
  items = [],
  renderItem,
  keyExtractor = (item, index) => item.id || item.key || index,
  itemEstimatedHeight = 120,
  buffer = 5,
  className = "",
  style = {}
}) {
  const containerRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [containerHeight, setContainerHeight] = useState(0);
  
  // Track heights of rendered items by unique key using a prototype-less cache to avoid inherited properties collisions
  const heightsRef = useRef(Object.create(null));
  const [updateTrigger, forceUpdate] = useState(0);
  const itemResizeObservers = useRef(new Map());

  // Handle scroll position
  const handleScroll = (e) => {
    setScrollTop(e.currentTarget.scrollTop);
  };

  // Measure container height
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const resizeObserver = new ResizeObserver((entries) => {
      if (entries[0]) {
        setContainerHeight(entries[0].contentRect.height || container.clientHeight);
      }
    });
    resizeObserver.observe(container);
    setContainerHeight(container.clientHeight);

    return () => {
      resizeObserver.disconnect();
    };
  }, []);

  // Compute positions of all items based on measured heights or estimation
  const { offsets, totalHeight } = useMemo(() => {
    const offsets = [];
    let currentOffset = 0;
    for (let i = 0; i < items.length; i++) {
      const itemKey = keyExtractor(items[i], i);
      offsets.push(currentOffset);
      const height = heightsRef.current[itemKey] ?? itemEstimatedHeight;
      currentOffset += height;
    }
    return { offsets, totalHeight: currentOffset };
  }, [items, keyExtractor, itemEstimatedHeight, updateTrigger]);

  // Find visible item range
  const { startIndex, endIndex } = useMemo(() => {
    let startIndex = 0;
    let endIndex = 0;

    for (let i = 0; i < offsets.length; i++) {
      const top = offsets[i];
      const itemKey = keyExtractor(items[i], i);
      const bottom = top + (heightsRef.current[itemKey] ?? itemEstimatedHeight);
      
      if (bottom >= scrollTop) {
        startIndex = i;
        break;
      }
    }

    endIndex = startIndex;
    for (let i = startIndex; i < offsets.length; i++) {
      const top = offsets[i];
      if (top > scrollTop + containerHeight) {
        endIndex = i;
        break;
      }
      endIndex = i;
    }

    const bufferedStart = Math.max(0, startIndex - buffer);
    const bufferedEnd = Math.min(items.length - 1, endIndex + buffer);

    return { startIndex: bufferedStart, endIndex: bufferedEnd };
  }, [offsets, scrollTop, containerHeight, items, buffer, itemEstimatedHeight, keyExtractor, updateTrigger]);

  const measureItem = (index, itemKey, node) => {
    if (!node) {
      if (itemResizeObservers.current.has(itemKey)) {
        itemResizeObservers.current.get(itemKey).disconnect();
        itemResizeObservers.current.delete(itemKey);
      }
      return;
    }

    if (!itemResizeObservers.current.has(itemKey)) {
      const observer = new ResizeObserver((entries) => {
        if (entries[0]) {
          const height = entries[0].borderBoxSize?.[0]?.blockSize || entries[0].contentRect.height || node.offsetHeight;
          if (height > 0 && heightsRef.current[itemKey] !== height) {
            heightsRef.current[itemKey] = height;
            forceUpdate((c) => c + 1);
          }
        }
      });
      observer.observe(node);
      itemResizeObservers.current.set(itemKey, observer);
    }
    
    const height = node.offsetHeight;
    if (height > 0 && heightsRef.current[itemKey] !== height) {
      heightsRef.current[itemKey] = height;
      Promise.resolve().then(() => {
        forceUpdate((c) => c + 1);
      });
    }
  };

  useEffect(() => {
    return () => {
      itemResizeObservers.current.forEach((obs) => obs.disconnect());
    };
  }, []);

  const visibleItems = [];
  for (let i = startIndex; i <= endIndex; i++) {
    if (i >= 0 && i < items.length) {
      const item = items[i];
      const itemKey = keyExtractor(item, i);
      visibleItems.push({
        index: i,
        item,
        key: itemKey,
        top: offsets[i]
      });
    }
  }

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className={`overflow-y-auto ${className}`}
      style={{ position: "relative", ...style }}
    >
      <div style={{ height: totalHeight, width: "100%", position: "relative" }}>
        {visibleItems.map(({ index, item, key, top }) => (
          <div
            key={key}
            ref={(node) => measureItem(index, key, node)}
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              width: "100%",
              transform: `translate3d(0, ${top}px, 0)`,
              boxSizing: "border-box"
            }}
          >
            {renderItem(item, index)}
          </div>
        ))}
      </div>
    </div>
  );
}
